//! `zicato-supervisor` entry point.

use clap::Parser;
use std::net::IpAddr;
use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;
use tokio::signal::unix::{signal, SignalKind};
use tokio::sync::broadcast;
use tracing::{error, info};
use zicato_supervisor::{action_log::WatchdogLog, log, reader, server, watchdog, watcher};

#[derive(Parser, Debug)]
#[command(
    name = "zicato-supervisor",
    version,
    about = "Watchdog + dashboard server for the zicato runtime state files."
)]
struct Cli {
    /// Path to the zicato workspace (default: ./.zicato)
    #[arg(long, default_value = ".zicato")]
    workspace: PathBuf,

    /// HTTP server port (default: 7920; +1 retry up to 7930).
    ///
    /// Distinct from the Python dashboard service's default (7892) so
    /// the watchdog's `/statusz` surface and the dashboard UI do not
    /// contend for the same port: when `zicato evolve` spawns both, a
    /// shared default would make the second binder walk `+1` and the
    /// reported URL point at the wrong server. The two walk ranges
    /// (7920-7930 here, 7892-7902 for the dashboard) are disjoint.
    #[arg(long, default_value_t = 7920)]
    port: u16,

    /// Bind address (default: 127.0.0.1)
    #[arg(long, default_value = "127.0.0.1")]
    bind: IpAddr,

    /// Disable control-file writing (POST endpoints return 403)
    #[arg(long, default_value_t = false)]
    read_only: bool,

    /// Run as the watchdog only: do not mount the dashboard UI, the
    /// analytical `/api/*` routes, the SSE stream, or the control
    /// endpoints. The watchdog's own `/statusz` surface stays available.
    #[arg(long, default_value_t = false)]
    no_dashboard: bool,

    /// Heartbeat staleness check interval (seconds)
    #[arg(long, default_value_t = 2)]
    interval: u64,

    /// Warn after this many seconds without heartbeat
    #[arg(long, default_value_t = 30)]
    heartbeat_stale_warn: u64,

    /// SIGKILL orchestrator after this many seconds without heartbeat
    #[arg(long, default_value_t = 90)]
    heartbeat_stale_kill: u64,

    /// Warn for stalled run after this many seconds
    #[arg(long, default_value_t = 30)]
    run_stale_warn: u64,

    /// Kill stalled run after this many seconds
    #[arg(long, default_value_t = 120)]
    run_stale_kill: u64,

    /// Disable per-run wall-clock deadline enforcement. Deadline killing
    /// is on by default; pass this to attach a read-only observability
    /// supervisor to a run it should not police.
    #[arg(long, default_value_t = false)]
    run_deadline_kill_disabled: bool,

    /// Grace window (seconds) between SIGTERM and SIGKILL when killing a
    /// run that has exceeded its wall-clock deadline.
    #[arg(long, default_value_t = 5)]
    run_kill_grace: u64,

    /// Log level (default: info)
    #[arg(long, default_value = "info")]
    log: String,

    /// Detach and run in the background (for `zicato dashboard --daemon`)
    #[arg(long, default_value_t = false)]
    daemon: bool,
}

#[tokio::main]
async fn main() -> std::process::ExitCode {
    let cli = Cli::parse();
    log::init(&cli.log);

    if cli.daemon {
        // Best-effort fork into background. We avoid pulling in a full
        // daemonization crate; the parent simply exits after kicking the
        // worker. Logs continue to stderr (the operator is expected to
        // redirect them).
        match daemonize() {
            Ok(true) => return std::process::ExitCode::SUCCESS,
            Ok(false) => {} // child continues
            Err(e) => {
                error!(error=%e, "daemonize failed; running in foreground");
            }
        }
    }

    let workspace = match cli.workspace.canonicalize() {
        Ok(p) => p,
        Err(_) => {
            // Workspace may not exist yet; create it.
            if let Err(e) = std::fs::create_dir_all(&cli.workspace) {
                error!(?cli.workspace, error=%e, "cannot create workspace");
                return std::process::ExitCode::FAILURE;
            }
            cli.workspace
                .canonicalize()
                .unwrap_or(cli.workspace.clone())
        }
    };

    let paths = reader::WorkspacePaths::new(workspace);
    info!(workspace=?paths.workspace, "starting zicato-supervisor");

    // Channels.
    let (watch_tx, _) = broadcast::channel::<watcher::WatchEvent>(256);
    let (shutdown_tx, _) = broadcast::channel::<()>(8);

    // Filesystem watcher (kept in scope for the program lifetime).
    let _watcher = match watcher::spawn(paths.clone(), watch_tx.clone(), Duration::from_millis(100))
    {
        Ok(w) => Some(w),
        Err(e) => {
            error!(error=%e, "failed to start filesystem watcher");
            None
        }
    };

    // Watchdog tasks.
    let thresholds = watchdog::Thresholds {
        heartbeat_stale_warn: Duration::from_secs(cli.heartbeat_stale_warn),
        heartbeat_stale_kill: Duration::from_secs(cli.heartbeat_stale_kill),
        run_stale_warn: Duration::from_secs(cli.run_stale_warn),
        run_stale_kill: Duration::from_secs(cli.run_stale_kill),
        grace: Duration::from_secs(5),
        run_kill_grace: Duration::from_secs(cli.run_kill_grace),
        run_deadline_kill_disabled: cli.run_deadline_kill_disabled,
    };
    if cli.run_deadline_kill_disabled {
        info!("per-run wall-clock deadline enforcement is disabled");
    }
    if cli.no_dashboard {
        info!("watchdog-only mode: dashboard routes disabled; /statusz still served");
    }

    // Shared in-memory ring buffer: the watchdog loops record their
    // escalations here, and `/statusz` reads them back.
    let action_log = Arc::new(WatchdogLog::new());

    // Shared heartbeat seq-liveness tracker: the heartbeat loop advances it
    // each tick; `/statusz` reads (does not advance) it to report the same
    // seq-change age the watchdog is deciding on.
    let seq_liveness = Arc::new(std::sync::Mutex::new(watchdog::SeqLiveness::new()));

    let interval = Duration::from_secs(cli.interval.max(1));
    let hb_paths = paths.clone();
    let hb_shutdown = shutdown_tx.clone();
    let hb_log = action_log.clone();
    let hb_seq = seq_liveness.clone();
    tokio::spawn(async move {
        watchdog::heartbeat_loop(hb_paths, thresholds, interval, hb_log, hb_seq, hb_shutdown).await
    });
    let run_paths = paths.clone();
    let run_shutdown = shutdown_tx.clone();
    let run_log = action_log.clone();
    tokio::spawn(async move {
        watchdog::runs_loop(run_paths, thresholds, interval, run_log, run_shutdown).await
    });

    // HTTP server.
    let handle = match server::serve(
        paths.clone(),
        cli.bind,
        cli.port,
        server::ServeOptions {
            read_only: cli.read_only,
            dashboard_disabled: cli.no_dashboard,
            heartbeat_stale_threshold_seconds: cli.heartbeat_stale_warn,
            action_log: action_log.clone(),
            seq_liveness: seq_liveness.clone(),
        },
        watch_tx.clone(),
        shutdown_tx.clone(),
    )
    .await
    {
        Ok(h) => h,
        Err(e) => {
            error!(error=%e, "failed to start HTTP server");
            return std::process::ExitCode::FAILURE;
        }
    };
    println!("zicato-supervisor listening on http://{}", handle.addr);

    // Wait for signals.
    let mut sigterm = match signal(SignalKind::terminate()) {
        Ok(s) => s,
        Err(e) => {
            error!(error=%e, "failed to install SIGTERM handler");
            return std::process::ExitCode::FAILURE;
        }
    };
    let mut sigint = match signal(SignalKind::interrupt()) {
        Ok(s) => s,
        Err(e) => {
            error!(error=%e, "failed to install SIGINT handler");
            return std::process::ExitCode::FAILURE;
        }
    };

    tokio::select! {
        _ = sigterm.recv() => info!("received SIGTERM; shutting down"),
        _ = sigint.recv() => info!("received SIGINT; shutting down"),
    }
    let _ = shutdown_tx.send(());
    // Give the server a moment to drain.
    tokio::time::sleep(Duration::from_millis(200)).await;
    std::process::ExitCode::SUCCESS
}

/// Best-effort daemonization. Returns `Ok(true)` in the parent (which
/// should exit immediately) and `Ok(false)` in the child.
fn daemonize() -> std::io::Result<bool> {
    // SAFETY: fork()/setsid() are required for daemonization. We do not
    // touch shared state between fork and exec; we are not threaded yet
    // when this runs.
    use std::os::unix::io::AsRawFd;
    let pid = unsafe { libc::fork() };
    if pid < 0 {
        return Err(std::io::Error::last_os_error());
    }
    if pid > 0 {
        // Parent.
        return Ok(true);
    }
    // Child: detach from controlling terminal.
    unsafe {
        if libc::setsid() < 0 {
            return Err(std::io::Error::last_os_error());
        }
        // Redirect stdin to /dev/null. Keep stdout/stderr so logs still flow.
        if let Ok(f) = std::fs::File::open("/dev/null") {
            let fd = f.as_raw_fd();
            libc::dup2(fd, libc::STDIN_FILENO);
        }
    }
    Ok(false)
}
