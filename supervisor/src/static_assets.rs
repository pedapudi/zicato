//! Compile-time embedding of `supervisor/static/`.
//!
//! The UI agent (R3-D) populates the directory. If it's empty at compile
//! time, `index.html` is missing and `/` returns 404 with a short
//! placeholder; once the agent's branch is merged the binary rebuild
//! picks up the real UI without code changes here.

use axum::body::Body;
use axum::http::{header, HeaderValue, StatusCode};
use axum::response::{IntoResponse, Response};
use include_dir::{include_dir, Dir};

static STATIC_DIR: Dir<'_> = include_dir!("$CARGO_MANIFEST_DIR/static");

const PLACEHOLDER_HTML: &str = r#"<!doctype html>
<html><head><meta charset="utf-8"><title>zicato-supervisor</title></head>
<body style="font-family:system-ui;padding:2rem;max-width:48rem">
<h1>zicato-supervisor</h1>
<p>The supervisor is running. The dashboard UI bundle was not present at
compile time. JSON endpoints under <code>/api/</code> and the
<code>/events</code> SSE stream are available.</p>
<ul>
  <li><a href="/api/state">/api/state</a></li>
  <li><a href="/api/health">/api/health</a></li>
</ul>
</body></html>
"#;

pub fn serve(path: &str) -> Response {
    let normalized = path.trim_start_matches('/');
    let lookup = if normalized.is_empty() {
        "index.html"
    } else {
        normalized
    };

    if let Some(file) = STATIC_DIR.get_file(lookup) {
        let mime = mime_guess::from_path(lookup).first_or_octet_stream();
        return (
            [(
                header::CONTENT_TYPE,
                HeaderValue::from_str(mime.as_ref())
                    .unwrap_or(HeaderValue::from_static("application/octet-stream")),
            )],
            file.contents(),
        )
            .into_response();
    }

    // Index-fallback: when the UI bundle is missing entirely, serve a
    // placeholder for the root path so operators see *something*.
    if lookup == "index.html" {
        return (
            StatusCode::OK,
            [(
                header::CONTENT_TYPE,
                HeaderValue::from_static("text/html; charset=utf-8"),
            )],
            Body::from(PLACEHOLDER_HTML),
        )
            .into_response();
    }

    (StatusCode::NOT_FOUND, "not found").into_response()
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::body::to_bytes;
    use axum::http::StatusCode;

    #[tokio::test]
    async fn missing_root_serves_placeholder() {
        let resp = serve("/");
        assert_eq!(resp.status(), StatusCode::OK);
        let bytes = to_bytes(resp.into_body(), 1 << 20).await.unwrap();
        let body = std::str::from_utf8(&bytes).unwrap();
        assert!(body.contains("zicato-supervisor"));
    }

    #[tokio::test]
    async fn unknown_static_is_404() {
        let resp = serve("/does-not-exist.css");
        assert_eq!(resp.status(), StatusCode::NOT_FOUND);
    }
}
