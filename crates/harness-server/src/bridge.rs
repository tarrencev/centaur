//! `client-tools-bridge`: a minimal stdio MCP server the sandbox harness
//! (claude/codex) spawns to reach the user's local tools.
//!
//! Flow: the model calls an MCP tool → claude/codex sends `tools/call` to this
//! bridge over stdio → the bridge relays it to the harness server over a unix
//! socket → the harness server emits the call to the Centaur client (which the
//! local CLI runs natively) and returns the steered `tool_result` → the bridge
//! returns it to claude/codex as the MCP tool result.
//!
//! The bridge inherits `CENTAUR_CLIENT_TOOLS` (it's a descendant of the harness
//! process), so it can answer `tools/list` itself. The pure request/response
//! builders are unit-tested here; the live stdio+socket loop is exercised in a
//! deployed sandbox.

use std::io::{BufRead, BufReader, Write};
use std::os::unix::net::UnixStream;

use serde_json::{Value, json};

use crate::client_tools::{self, ClientTool};
use crate::error::Result;

const MCP_PROTOCOL_VERSION: &str = "2024-11-05";

/// Run the bridge: read MCP JSON-RPC from stdin, relay tool calls to `socket_path`.
pub fn run(socket_path: &str) -> Result<()> {
    let tools = client_tools::from_env();
    let stdin = std::io::stdin();
    let mut stdout = std::io::stdout();
    let mut call_seq: u64 = 0;

    for line in stdin.lock().lines() {
        let line = line?;
        let trimmed = line.trim();
        if trimmed.is_empty() {
            continue;
        }
        let Ok(request) = serde_json::from_str::<Value>(trimmed) else {
            continue;
        };
        let Some(response) = dispatch(&request, &tools, socket_path, &mut call_seq) else {
            continue; // notification: no response
        };
        writeln!(stdout, "{}", serde_json::to_string(&response)?)?;
        stdout.flush()?;
    }
    Ok(())
}

/// Route one JSON-RPC request to its response. Returns `None` for notifications.
fn dispatch(
    request: &Value,
    tools: &[ClientTool],
    socket_path: &str,
    call_seq: &mut u64,
) -> Option<Value> {
    let id = request.get("id").cloned();
    let method = request.get("method").and_then(Value::as_str)?;
    match method {
        "initialize" => Some(initialize_response(id?)),
        "tools/list" => Some(tools_list_response(id?, tools)),
        "ping" => Some(result_response(id?, json!({}))),
        "tools/call" => {
            let id = id?;
            let params = request.get("params")?;
            let name = params.get("name").and_then(Value::as_str)?.to_owned();
            let arguments = params
                .get("arguments")
                .cloned()
                .unwrap_or_else(|| json!({}));
            *call_seq += 1;
            let relay = relay_call(socket_path, *call_seq, &name, &arguments);
            Some(tool_call_response(id, relay))
        }
        // notifications/initialized, notifications/cancelled, etc.
        _ => None,
    }
}

fn initialize_response(id: Value) -> Value {
    result_response(
        id,
        json!({
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": { "tools": {} },
            "serverInfo": { "name": client_tools::BRIDGE_SERVER_NAME, "version": "0.1.0" },
        }),
    )
}

fn tools_list_response(id: Value, tools: &[ClientTool]) -> Value {
    let listed = tools
        .iter()
        .map(|tool| {
            json!({
                "name": tool.name,
                "description": tool.description,
                "inputSchema": tool.input_schema,
            })
        })
        .collect::<Vec<_>>();
    result_response(id, json!({ "tools": listed }))
}

/// Shape the MCP `tools/call` result from a relay outcome.
fn tool_call_response(id: Value, relay: RelayOutcome) -> Value {
    result_response(
        id,
        json!({
            "content": [{ "type": "text", "text": relay.text }],
            "isError": relay.is_error,
        }),
    )
}

fn result_response(id: Value, result: Value) -> Value {
    json!({ "jsonrpc": "2.0", "id": id, "result": result })
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct RelayOutcome {
    text: String,
    is_error: bool,
}

/// The newline-delimited JSON sent bridge → harness server for one tool call.
fn relay_request_line(call_id: u64, name: &str, arguments: &Value) -> String {
    json!({ "type": "call", "id": call_id, "name": name, "arguments": arguments }).to_string()
}

/// Parse the harness server's `{type:"result", ...}` reply line.
fn parse_relay_response(line: &str) -> Option<RelayOutcome> {
    let value = serde_json::from_str::<Value>(line.trim()).ok()?;
    if value.get("type").and_then(Value::as_str) != Some("result") {
        return None;
    }
    Some(RelayOutcome {
        text: value
            .get("content")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_owned(),
        is_error: value.get("is_error").and_then(Value::as_bool).unwrap_or(false),
    })
}

/// One synchronous round-trip to the harness server over the unix socket. On any
/// IO error, surfaces an error tool result so the model isn't left hanging.
fn relay_call(socket_path: &str, call_id: u64, name: &str, arguments: &Value) -> RelayOutcome {
    match relay_call_inner(socket_path, call_id, name, arguments) {
        Ok(outcome) => outcome,
        Err(error) => RelayOutcome {
            text: format!("client tool bridge error: {error}"),
            is_error: true,
        },
    }
}

fn relay_call_inner(
    socket_path: &str,
    call_id: u64,
    name: &str,
    arguments: &Value,
) -> std::io::Result<RelayOutcome> {
    let mut stream = UnixStream::connect(socket_path)?;
    let mut line = relay_request_line(call_id, name, arguments);
    line.push('\n');
    stream.write_all(line.as_bytes())?;
    stream.flush()?;

    let mut reader = BufReader::new(stream);
    let mut response = String::new();
    reader.read_line(&mut response)?;
    Ok(parse_relay_response(&response).unwrap_or(RelayOutcome {
        text: "client tool bridge: malformed relay response".to_owned(),
        is_error: true,
    }))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tool(name: &str) -> ClientTool {
        ClientTool {
            name: name.to_owned(),
            description: format!("desc {name}"),
            input_schema: json!({ "type": "object" }),
        }
    }

    #[test]
    fn initialize_advertises_tools_capability() {
        let resp = initialize_response(json!(1));
        assert_eq!(resp["id"], 1);
        assert_eq!(resp["result"]["protocolVersion"], MCP_PROTOCOL_VERSION);
        assert!(resp["result"]["capabilities"]["tools"].is_object());
        assert_eq!(resp["result"]["serverInfo"]["name"], "centaur_local");
    }

    #[test]
    fn tools_list_uses_mcp_input_schema_key() {
        let resp = tools_list_response(json!(2), &[tool("Bash"), tool("Read")]);
        let listed = resp["result"]["tools"].as_array().unwrap();
        assert_eq!(listed.len(), 2);
        assert_eq!(listed[0]["name"], "Bash");
        assert_eq!(listed[0]["description"], "desc Bash");
        // MCP uses `inputSchema` (camelCase), not Anthropic's `input_schema`.
        assert!(listed[0]["inputSchema"].is_object());
    }

    #[test]
    fn dispatch_returns_none_for_notifications() {
        let req = json!({ "jsonrpc": "2.0", "method": "notifications/initialized" });
        let mut seq = 0;
        assert!(dispatch(&req, &[], "/tmp/none", &mut seq).is_none());
    }

    #[test]
    fn tool_call_response_wraps_text_content() {
        let resp = tool_call_response(
            json!(7),
            RelayOutcome { text: "hi".into(), is_error: false },
        );
        assert_eq!(resp["id"], 7);
        assert_eq!(resp["result"]["content"][0]["type"], "text");
        assert_eq!(resp["result"]["content"][0]["text"], "hi");
        assert_eq!(resp["result"]["isError"], false);
    }

    #[test]
    fn relay_request_and_response_round_trip_shapes() {
        let line = relay_request_line(3, "Bash", &json!({ "command": "uname -a" }));
        let parsed: Value = serde_json::from_str(&line).unwrap();
        assert_eq!(parsed["type"], "call");
        assert_eq!(parsed["id"], 3);
        assert_eq!(parsed["name"], "Bash");
        assert_eq!(parsed["arguments"]["command"], "uname -a");

        let outcome =
            parse_relay_response(r#"{"type":"result","content":"Linux","is_error":false}"#).unwrap();
        assert_eq!(outcome, RelayOutcome { text: "Linux".into(), is_error: false });

        assert!(parse_relay_response(r#"{"type":"other"}"#).is_none());
    }
}
