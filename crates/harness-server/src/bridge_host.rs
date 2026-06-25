//! Harness-server side of client-tool passthrough: a unix-socket listener the
//! [`crate::bridge`] subprocess relays tool calls to.
//!
//! When the sandbox model calls a client (forward-only) MCP tool, the bridge
//! relays `{type:"call", id, name, arguments}` here. The host:
//!   1. assigns a `tool_use_id`, queues a [`PendingCall`], and parks the
//!      bridge's connection keyed by that id;
//!   2. the turn loop drains [`BridgeHost::take_pending`] and emits each as a
//!      `tool_use` to the Centaur client (which the local CLI runs natively);
//!   3. the local CLI's `tool_result` returns as a steered message; the steer
//!      handler calls [`BridgeHost::complete`], which writes
//!      `{type:"result", ...}` back to the parked connection → the bridge →
//!      the model.
//!
//! The id correlation across the harness↔ingress↔local-CLI boundary is the part
//! validated in a deployed sandbox; the listener + rendezvous bookkeeping here is
//! deterministic and unit-tested in-process via socket pairs.

use std::collections::VecDeque;
use std::io::{BufRead, BufReader, Write};
use std::os::unix::net::{UnixListener, UnixStream};
use std::sync::{Arc, Mutex};
use std::thread;

use serde_json::{Value, json};
use uuid::Uuid;

/// A client tool call awaiting emission to the Centaur client.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PendingCall {
    /// Correlation id we mint; becomes the emitted `tool_use` id and the id the
    /// local CLI echoes back in its `tool_result`.
    pub tool_use_id: String,
    /// The client's original tool name (e.g. `Bash`), un-namespaced.
    pub name: String,
    pub arguments: Value,
}

#[derive(Default)]
struct Rendezvous {
    /// Calls received from bridges, not yet emitted.
    queue: VecDeque<PendingCall>,
    /// Parked bridge connections keyed by `tool_use_id`, awaiting a result.
    parked: Vec<(String, UnixStream)>,
}

/// Listens for bridge connections and tracks in-flight client tool calls.
pub struct BridgeHost {
    socket_path: String,
    inner: Arc<Mutex<Rendezvous>>,
}

impl BridgeHost {
    /// Bind the listener and spawn the accept loop. The socket path is what the
    /// bridge subprocess (and `CENTAUR_CLIENT_TOOLS_SOCKET`) point at.
    pub fn bind(socket_path: impl Into<String>) -> std::io::Result<Self> {
        let socket_path = socket_path.into();
        let _ = std::fs::remove_file(&socket_path); // clear a stale socket
        let listener = UnixListener::bind(&socket_path)?;
        let inner = Arc::new(Mutex::new(Rendezvous::default()));
        let accept_inner = Arc::clone(&inner);
        thread::spawn(move || {
            for stream in listener.incoming() {
                let Ok(stream) = stream else { continue };
                if let Err(error) = Self::accept_call(&accept_inner, stream) {
                    eprintln!("client-tools bridge host: {error}");
                }
            }
        });
        Ok(Self { socket_path, inner })
    }

    /// Read one `{type:"call", ...}` from a bridge connection, queue it, and park
    /// the connection for its eventual result.
    fn accept_call(inner: &Arc<Mutex<Rendezvous>>, stream: UnixStream) -> std::io::Result<()> {
        let mut reader = BufReader::new(stream.try_clone()?);
        let mut line = String::new();
        reader.read_line(&mut line)?;
        let Some(call) = parse_call(&line) else {
            return Ok(()); // ignore malformed; connection drops
        };
        let tool_use_id = format!("ctl_{}", Uuid::new_v4().simple());
        let pending = PendingCall {
            tool_use_id: tool_use_id.clone(),
            name: call.name,
            arguments: call.arguments,
        };
        let mut guard = inner.lock().expect("bridge host mutex");
        guard.queue.push_back(pending);
        guard.parked.push((tool_use_id, stream));
        Ok(())
    }

    /// Drain calls awaiting emission to the Centaur client.
    pub fn take_pending(&self) -> Vec<PendingCall> {
        let mut guard = self.inner.lock().expect("bridge host mutex");
        guard.queue.drain(..).collect()
    }

    /// Whether `tool_use_id` is an in-flight client tool call (so the steer
    /// handler should divert its `tool_result` here instead of to harness stdin).
    pub fn is_tracked(&self, tool_use_id: &str) -> bool {
        let guard = self.inner.lock().expect("bridge host mutex");
        guard.parked.iter().any(|(id, _)| id == tool_use_id)
    }

    /// Return a tool result to the parked bridge connection, unblocking the model.
    /// Returns `true` if the id was tracked.
    pub fn complete(&self, tool_use_id: &str, content: &str, is_error: bool) -> bool {
        let mut stream = {
            let mut guard = self.inner.lock().expect("bridge host mutex");
            let Some(pos) = guard.parked.iter().position(|(id, _)| id == tool_use_id) else {
                return false;
            };
            guard.parked.swap_remove(pos).1
        };
        let mut line = json!({ "type": "result", "content": content, "is_error": is_error }).to_string();
        line.push('\n');
        if let Err(error) = stream.write_all(line.as_bytes()).and_then(|()| stream.flush()) {
            eprintln!("client-tools bridge host: failed to return result: {error}");
        }
        true
    }

    pub fn socket_path(&self) -> &str {
        &self.socket_path
    }
}

impl Drop for BridgeHost {
    fn drop(&mut self) {
        let _ = std::fs::remove_file(&self.socket_path);
    }
}

struct IncomingCall {
    name: String,
    arguments: Value,
}

fn parse_call(line: &str) -> Option<IncomingCall> {
    let value = serde_json::from_str::<Value>(line.trim()).ok()?;
    if value.get("type").and_then(Value::as_str) != Some("call") {
        return None;
    }
    let name = value.get("name").and_then(Value::as_str)?.to_owned();
    let arguments = value.get("arguments").cloned().unwrap_or_else(|| json!({}));
    Some(IncomingCall { name, arguments })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn unique_socket(tag: &str) -> String {
        format!("/tmp/centaur-bridge-test-{tag}-{}.sock", Uuid::new_v4().simple())
    }

    #[test]
    fn parse_call_validates_shape() {
        let call = parse_call(r#"{"type":"call","id":1,"name":"Bash","arguments":{"command":"ls"}}"#)
            .expect("valid call");
        assert_eq!(call.name, "Bash");
        assert_eq!(call.arguments["command"], "ls");
        assert!(parse_call(r#"{"type":"result"}"#).is_none());
        assert!(parse_call("garbage").is_none());
    }

    #[test]
    fn end_to_end_call_queue_and_complete() {
        let socket = unique_socket("e2e");
        let host = BridgeHost::bind(&socket).expect("bind");

        // Simulate the bridge: connect and send a call line.
        let mut client = UnixStream::connect(&socket).expect("connect");
        client
            .write_all(b"{\"type\":\"call\",\"id\":1,\"name\":\"Bash\",\"arguments\":{\"command\":\"uname\"}}\n")
            .unwrap();
        client.flush().unwrap();

        // The host should surface it as a pending call (poll briefly for the
        // accept thread).
        let pending = wait_for_pending(&host);
        assert_eq!(pending.name, "Bash");
        assert_eq!(pending.arguments["command"], "uname");
        assert!(host.is_tracked(&pending.tool_use_id));

        // Completing it should write a result back to the bridge connection.
        assert!(host.complete(&pending.tool_use_id, "Linux", false));
        assert!(!host.is_tracked(&pending.tool_use_id));

        let mut reader = BufReader::new(client);
        let mut response = String::new();
        reader.read_line(&mut response).unwrap();
        let value: Value = serde_json::from_str(response.trim()).unwrap();
        assert_eq!(value["type"], "result");
        assert_eq!(value["content"], "Linux");
        assert_eq!(value["is_error"], false);
    }

    #[test]
    fn complete_unknown_id_is_false() {
        let socket = unique_socket("unknown");
        let host = BridgeHost::bind(&socket).expect("bind");
        assert!(!host.complete("ctl_missing", "x", false));
    }

    fn wait_for_pending(host: &BridgeHost) -> PendingCall {
        for _ in 0..200 {
            if let Some(call) = host.take_pending().into_iter().next() {
                return call;
            }
            thread::sleep(std::time::Duration::from_millis(5));
        }
        panic!("no pending call surfaced");
    }
}
