//! Isolated process controller that adopts and reaps its own descendants.

use std::io::BufRead;
use std::process::{Child, Command, Stdio};

pub(crate) struct OwnedGroup {
    controller: Child,
    pub leader: i32,
    pub descendant: i32,
    pub start_time: f64,
}

impl OwnedGroup {
    pub fn spawn(leader_exited: bool) -> Self {
        let script = r#"
import ctypes, json, os, signal, sys, threading
from pathlib import Path
assert ctypes.CDLL(None).prctl(36, 1, 0, 0, 0) == 0
read_fd, write_fd = os.pipe()
leader = os.fork()
if leader == 0:
    os.close(read_fd)
    os.setsid()
    child = os.fork()
    if child == 0:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        os.write(write_fd, str(os.getpid()).encode())
    os.close(write_fd)
    while True:
        signal.pause()
os.close(write_fd)
child = int(os.read(read_fd, 100))
os.close(read_fd)
raw = Path(f'/proc/{leader}/stat').read_text()
token = float(raw[raw.rindex(')') + 1:].split()[19])
reaper = threading.Thread(target=lambda: os.waitpid(leader, 0))
reaper.start()
if sys.argv[1] == 'exited':
    os.kill(leader, signal.SIGTERM)
    reaper.join()
print(json.dumps([leader, child, token]), flush=True)
sys.stdin.readline()
try:
    os.killpg(leader, signal.SIGKILL)
except ProcessLookupError:
    pass
reaper.join()
while True:
    try:
        os.waitpid(-1, 0)
    except ChildProcessError:
        break
"#;
        let mut controller = Command::new("python3")
            .args(["-c", script, if leader_exited { "exited" } else { "alive" }])
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .spawn()
            .unwrap();
        let mut ready = String::new();
        std::io::BufReader::new(controller.stdout.take().unwrap())
            .read_line(&mut ready)
            .unwrap();
        let (leader, descendant, start_time) = serde_json::from_str(&ready).unwrap();
        Self {
            controller,
            leader,
            descendant,
            start_time,
        }
    }
}

impl Drop for OwnedGroup {
    fn drop(&mut self) {
        self.controller.stdin.take();
        let _ = self.controller.wait();
    }
}
