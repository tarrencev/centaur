//! Client-side (forward-only) tool support.
//!
//! When a local `claude`/`codex` CLI drives Centaur through the `/v1` ingress, it
//! advertises its own tools (its `Bash`/shell, `Read`, `Edit`, …) in
//! `request.tools`. The ingress serializes that manifest into the
//! `CENTAUR_CLIENT_TOOLS` env var (see api-server `client_tools_json`). Those
//! tools must NOT run in the sandbox — they run on the user's machine. We expose
//! them to the sandbox harness as **MCP tools** backed by an in-process bridge
//! (see [`crate::bridge`]): when the model calls one, the bridge emits the call
//! out to the Centaur client (which the local CLI executes natively) and resumes
//! on the steered `tool_result`.
//!
//! This module is the deterministic core: parsing the manifest and generating the
//! per-harness MCP wiring. It is fully unit-tested; the live bridge transport is
//! verified in a deployed sandbox.

use std::env;

use serde_json::{Value, json};

/// The MCP server name the bridge is registered under. Claude surfaces MCP tools
/// to the model as `mcp__<server>__<tool>`, so this is part of the tool name the
/// model sees and the harness must recognize.
pub const BRIDGE_SERVER_NAME: &str = "centaur_local";

/// Env var the ingress sets with the client's advertised tool manifest (verbatim
/// `request.tools`, Anthropic- or OpenAI-shaped).
pub const CLIENT_TOOLS_ENV: &str = "CENTAUR_CLIENT_TOOLS";

/// Env var the harness server sets to the bridge unix-socket path once its
/// listener is live. While unset, client-tool wiring stays dormant and the
/// harness behaves exactly as before — so partial deploys are safe.
pub const BRIDGE_SOCKET_ENV: &str = "CENTAUR_CLIENT_TOOLS_SOCKET";

/// The live bridge socket path, or `None` when the server-side listener is not up.
pub fn bridge_socket_path() -> Option<String> {
    env::var(BRIDGE_SOCKET_ENV)
        .ok()
        .map(|value| value.trim().to_owned())
        .filter(|value| !value.is_empty())
}

/// `(command, args)` launching the bridge MCP server as a subprocess of the
/// harness CLI, pointed at `socket_path`. Uses the current executable so the
/// `client-tools-bridge` subcommand is always reachable.
pub fn bridge_command(socket_path: &str) -> (String, Vec<String>) {
    let exe = env::current_exe()
        .ok()
        .and_then(|path| path.to_str().map(str::to_owned))
        .unwrap_or_else(|| "harness-server".to_owned());
    (
        exe,
        vec![
            "client-tools-bridge".to_owned(),
            "--socket".to_owned(),
            socket_path.to_owned(),
        ],
    )
}

/// Whether client-tool passthrough should be wired for this turn: the ingress
/// advertised tools AND the server-side bridge listener is live.
pub fn active() -> Option<(Vec<ClientTool>, String)> {
    let socket = bridge_socket_path()?;
    let tools = from_env();
    (!tools.is_empty()).then_some((tools, socket))
}

/// A single client-advertised tool, normalized across the Anthropic
/// (`{name, description, input_schema}`) and OpenAI
/// (`{type:"function", name, description, parameters}` or nested `function`)
/// shapes.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ClientTool {
    pub name: String,
    pub description: String,
    pub input_schema: Value,
}

impl ClientTool {
    /// The name the sandbox harness exposes to the model (MCP-namespaced).
    pub fn mcp_name(&self) -> String {
        format!("mcp__{BRIDGE_SERVER_NAME}__{}", self.name)
    }
}

/// Parse [`CLIENT_TOOLS_ENV`] into normalized tools. Returns empty when unset,
/// blank, or not a JSON array — the harness then behaves exactly as before.
pub fn from_env() -> Vec<ClientTool> {
    match env::var(CLIENT_TOOLS_ENV) {
        Ok(raw) => parse(&raw),
        Err(_) => Vec::new(),
    }
}

/// Parse a JSON tool manifest. Tolerant of both wire shapes; skips malformed
/// entries rather than failing the whole turn.
pub fn parse(raw: &str) -> Vec<ClientTool> {
    let raw = raw.trim();
    if raw.is_empty() {
        return Vec::new();
    }
    let Ok(Value::Array(items)) = serde_json::from_str::<Value>(raw) else {
        return Vec::new();
    };
    items.iter().filter_map(parse_one).collect()
}

fn parse_one(value: &Value) -> Option<ClientTool> {
    // OpenAI function tools may nest under `function`; Anthropic + flat OpenAI
    // keep fields at the top level.
    let function = value.get("function");
    let scope = function.unwrap_or(value);

    let name = scope
        .get("name")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|name| !name.is_empty())?
        .to_owned();

    let description = scope
        .get("description")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_owned();

    // Anthropic: `input_schema`; OpenAI: `parameters`. Default to an open object.
    let input_schema = scope
        .get("input_schema")
        .or_else(|| scope.get("parameters"))
        .cloned()
        .unwrap_or_else(|| json!({ "type": "object" }));

    Some(ClientTool {
        name,
        description,
        input_schema,
    })
}

/// `true` when `tool_name` (as the model called it) is a bridge MCP tool, i.e.
/// `mcp__centaur_local__<original>`.
pub fn is_client_tool(tool_name: &str) -> bool {
    strip_mcp_prefix(tool_name).is_some()
}

/// Recover the client's original tool name (e.g. `Bash`) from the MCP-namespaced
/// name the model used (e.g. `mcp__centaur_local__Bash`). The original name is
/// what we emit to the Centaur client so the local CLI runs its native tool.
pub fn original_tool_name(tool_name: &str) -> Option<&str> {
    strip_mcp_prefix(tool_name)
}

fn strip_mcp_prefix(tool_name: &str) -> Option<&str> {
    tool_name.strip_prefix(&format!("mcp__{BRIDGE_SERVER_NAME}__"))
}

/// Build the Claude `--mcp-config` JSON registering the bridge as a stdio MCP
/// server. `bridge_command`/`bridge_args` launch [`crate::bridge`] pointed at the
/// per-turn unix socket via `socket_path` (passed through bridge args).
pub fn claude_mcp_config(bridge_command: &str, bridge_args: &[String]) -> Value {
    json!({
        "mcpServers": {
            BRIDGE_SERVER_NAME: {
                "command": bridge_command,
                "args": bridge_args,
            }
        }
    })
}

/// The `--allowedTools` value permitting every bridge MCP tool, so
/// `bypassPermissions` mode still surfaces them.
pub fn claude_allowed_tools() -> String {
    format!("mcp__{BRIDGE_SERVER_NAME}__*")
}

/// Build the codex config overlay (TOML) registering the bridge as a stdio MCP
/// server under `[mcp_servers.<name>]`, appended to the harness config overlay.
pub fn codex_mcp_config_toml(bridge_command: &str, bridge_args: &[String]) -> String {
    let args = bridge_args
        .iter()
        .map(|arg| format!("{arg:?}")) // debug-quote -> valid TOML basic string
        .collect::<Vec<_>>()
        .join(", ");
    format!(
        "\n[mcp_servers.{BRIDGE_SERVER_NAME}]\ncommand = {bridge_command:?}\nargs = [{args}]\n"
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_anthropic_shape() {
        let raw = r#"[{"name":"Bash","description":"run a shell command","input_schema":{"type":"object","properties":{"command":{"type":"string"}}}}]"#;
        let tools = parse(raw);
        assert_eq!(tools.len(), 1);
        assert_eq!(tools[0].name, "Bash");
        assert_eq!(tools[0].description, "run a shell command");
        assert_eq!(tools[0].input_schema["properties"]["command"]["type"], "string");
    }

    #[test]
    fn parses_openai_function_shapes() {
        // Flat OpenAI tool.
        let flat = r#"[{"type":"function","name":"read_file","parameters":{"type":"object"}}]"#;
        assert_eq!(parse(flat)[0].name, "read_file");
        // Nested-under-`function` OpenAI tool.
        let nested = r#"[{"type":"function","function":{"name":"edit","description":"d","parameters":{"type":"object"}}}]"#;
        let tools = parse(nested);
        assert_eq!(tools[0].name, "edit");
        assert_eq!(tools[0].description, "d");
    }

    #[test]
    fn empty_and_malformed_yield_no_tools() {
        assert!(parse("").is_empty());
        assert!(parse("   ").is_empty());
        assert!(parse("not json").is_empty());
        assert!(parse("{}").is_empty()); // object, not array
        assert!(parse(r#"[{"description":"no name"}]"#).is_empty());
    }

    #[test]
    fn defaults_missing_schema_to_open_object() {
        let tools = parse(r#"[{"name":"X"}]"#);
        assert_eq!(tools[0].input_schema, json!({"type":"object"}));
    }

    #[test]
    fn mcp_name_round_trips() {
        let tool = ClientTool {
            name: "Bash".into(),
            description: String::new(),
            input_schema: json!({}),
        };
        let mcp = tool.mcp_name();
        assert_eq!(mcp, "mcp__centaur_local__Bash");
        assert!(is_client_tool(&mcp));
        assert_eq!(original_tool_name(&mcp), Some("Bash"));
        assert!(!is_client_tool("Bash"));
        assert_eq!(original_tool_name("Read"), None);
    }

    #[test]
    fn claude_config_shape() {
        let cfg = claude_mcp_config("harness-server", &["client-tools-bridge".into(), "--socket".into(), "/tmp/s".into()]);
        assert_eq!(cfg["mcpServers"]["centaur_local"]["command"], "harness-server");
        assert_eq!(cfg["mcpServers"]["centaur_local"]["args"][0], "client-tools-bridge");
        assert_eq!(claude_allowed_tools(), "mcp__centaur_local__*");
    }

    #[test]
    fn codex_config_toml_is_valid() {
        let toml = codex_mcp_config_toml("harness-server", &["client-tools-bridge".into(), "--socket".into(), "/tmp/s".into()]);
        assert!(toml.contains("[mcp_servers.centaur_local]"));
        assert!(toml.contains(r#"command = "harness-server""#));
        assert!(toml.contains(r#"args = ["client-tools-bridge", "--socket", "/tmp/s"]"#));
    }
}
