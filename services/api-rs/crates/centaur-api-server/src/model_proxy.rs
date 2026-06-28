//! Reverse-proxy for the sandbox agent's model calls, with optional injection
//! and routing of "local" tools.
//!
//! Step 1 (transparent pass-through): `/sandbox/model/<rest>` ->
//! `CENTAUR_SANDBOX_MODEL_UPSTREAM`/<rest> (default hydra's `backend-api/codex`).
//! Replaces the incoming `Authorization` with the real `HYDRA_API_KEY` (the
//! sandbox only carries a placeholder), forwards method/body/headers, and
//! streams the (SSE) response straight back.
//!
//! Step 2 (this module): for the OpenAI Responses wire format (the body posted
//! to `.../responses`), when `CENTAUR_SANDBOX_LOCAL_TOOLS_STUB` is set we inject
//! "local" tools into the request and route their tool-calls through a buffering
//! re-query sub-loop, so the sandbox agent (codex) never sees those tools or
//! their calls. Everything else keeps the byte-for-byte transparent pass-through.

use std::env;

use axum::{
    body::{Body, Bytes},
    extract::Request,
    http::{HeaderMap, Method, StatusCode},
    response::{IntoResponse, Response},
};
use serde_json::{Value, json};

/// Shared client for the sandbox model-proxy.
static MODEL_PROXY_CLIENT: std::sync::OnceLock<reqwest::Client> = std::sync::OnceLock::new();

/// Prefix that tags local tool names so they're identifiable in the model's
/// function-call output and can be routed through the local sub-loop instead of
/// being surfaced to the sandbox agent.
const LOCAL_TOOL_PREFIX: &str = "local__";

/// Upper bound on local-tool re-query iterations before we bail with an error,
/// to avoid an unbounded loop if the model keeps calling local tools.
const MAX_SUBLOOP_ITERATIONS: usize = 16;

/// A function call extracted from a Responses SSE stream.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FunctionCall {
    pub name: String,
    pub call_id: String,
    /// The raw arguments JSON string (Responses transmits function-call
    /// arguments as a JSON-encoded string, streamed as deltas).
    pub arguments: String,
}

/// Reverse-proxy entrypoint for `/sandbox/model/<rest>`.
///
/// Routes to the transparent streaming pass-through (step 1) unless the request
/// targets the Responses `.../responses` endpoint AND
/// `CENTAUR_SANDBOX_LOCAL_TOOLS_STUB` is set, in which case it engages the
/// local-tool buffering sub-loop (step 2).
pub async fn proxy_sandbox_model(req: Request) -> Response {
    let (parts, body) = req.into_parts();
    let path_and_query = parts
        .uri
        .path_and_query()
        .map(|pq| pq.as_str())
        .unwrap_or("/");
    let rest = path_and_query
        .strip_prefix("/sandbox/model")
        .unwrap_or(path_and_query);
    let upstream = env::var("CENTAUR_SANDBOX_MODEL_UPSTREAM")
        .unwrap_or_else(|_| "https://hydra.64.34.84.225.sslip.io/backend-api/codex".to_string());
    let url = format!("{}{}", upstream.trim_end_matches('/'), rest);

    let body_bytes = match axum::body::to_bytes(body, usize::MAX).await {
        Ok(bytes) => bytes,
        Err(err) => {
            return (StatusCode::BAD_REQUEST, format!("model proxy: bad body: {err}")).into_response();
        }
    };

    // Only the Responses wire format on `.../responses` with the stub flag set
    // takes the buffering sub-loop path; everything else is the transparent
    // streaming pass-through from step 1 (byte-for-byte unchanged).
    let is_responses = parts.uri.path().trim_end_matches('/').ends_with("/responses");
    let stub_enabled = env::var("CENTAUR_SANDBOX_LOCAL_TOOLS_STUB")
        .map(|v| !v.trim().is_empty())
        .unwrap_or(false);

    if is_responses && stub_enabled {
        proxy_with_local_tools(&parts.method, &url, &parts.headers, body_bytes).await
    } else {
        transparent_passthrough(&parts.method, &url, &parts.headers, body_bytes).await
    }
}

/// Build the upstream request: forward method/headers/body, drop hop-by-hop and
/// auth headers, and inject the real `HYDRA_API_KEY` as a Bearer token.
fn build_outbound(
    method: &Method,
    url: &str,
    headers: &HeaderMap,
    body: Vec<u8>,
) -> reqwest::RequestBuilder {
    let client = MODEL_PROXY_CLIENT.get_or_init(reqwest::Client::new);
    let mut outbound = client.request(method.clone(), url).body(body);
    for (name, value) in headers.iter() {
        match name.as_str() {
            "host" | "authorization" | "content-length" => continue,
            _ => outbound = outbound.header(name.clone(), value.clone()),
        }
    }
    if let Ok(key) = env::var("HYDRA_API_KEY") {
        let key = key.trim();
        if !key.is_empty() {
            outbound = outbound.header("authorization", format!("Bearer {key}"));
        }
    }
    outbound
}

/// Step-1 behavior: stream the upstream (SSE) response straight back to the
/// caller, unchanged.
async fn transparent_passthrough(
    method: &Method,
    url: &str,
    headers: &HeaderMap,
    body: Bytes,
) -> Response {
    let outbound = build_outbound(method, url, headers, body.to_vec());
    let upstream_resp = match outbound.send().await {
        Ok(resp) => resp,
        Err(err) => {
            tracing::error!(error = %err, url = %url, "sandbox model proxy upstream error");
            return (StatusCode::BAD_GATEWAY, format!("model proxy upstream error: {err}"))
                .into_response();
        }
    };

    let status = upstream_resp.status();
    let mut builder = Response::builder().status(status.as_u16());
    for (name, value) in upstream_resp.headers().iter() {
        match name.as_str() {
            "content-length" | "transfer-encoding" | "connection" => continue,
            _ => builder = builder.header(name.clone(), value.clone()),
        }
    }
    match builder.body(Body::from_stream(upstream_resp.bytes_stream())) {
        Ok(resp) => resp,
        Err(err) => {
            tracing::error!(error = %err, "sandbox model proxy response build failed");
            (
                StatusCode::INTERNAL_SERVER_ERROR,
                "model proxy response build failed",
            )
                .into_response()
        }
    }
}

/// Step-2 behavior: inject local tools into the Responses request, buffer the
/// upstream SSE, and re-query with local tool results until the model stops
/// calling local tools (or a sandbox call / message is produced).
async fn proxy_with_local_tools(
    method: &Method,
    url: &str,
    headers: &HeaderMap,
    body_bytes: Bytes,
) -> Response {
    let mut request_body: Value = match serde_json::from_slice(&body_bytes) {
        Ok(value) => value,
        Err(err) => {
            // Body isn't JSON we can manipulate; never break a request we don't
            // understand — fall back to the transparent pass-through.
            tracing::warn!(error = %err, "sandbox model proxy: /responses body not JSON; passing through");
            return transparent_passthrough(method, url, headers, body_bytes).await;
        }
    };

    inject_local_tools(&mut request_body);

    for _ in 0..MAX_SUBLOOP_ITERATIONS {
        let serialized = match serde_json::to_vec(&request_body) {
            Ok(bytes) => bytes,
            Err(err) => {
                return (
                    StatusCode::INTERNAL_SERVER_ERROR,
                    format!("model proxy: serialize request: {err}"),
                )
                    .into_response();
            }
        };

        let outbound = build_outbound(method, url, headers, serialized);
        let upstream_resp = match outbound.send().await {
            Ok(resp) => resp,
            Err(err) => {
                tracing::error!(error = %err, url = %url, "sandbox model proxy upstream error");
                return (StatusCode::BAD_GATEWAY, format!("model proxy upstream error: {err}"))
                    .into_response();
            }
        };

        // Capture status + forwardable headers before consuming the body.
        let status = upstream_resp.status();
        let resp_headers = upstream_resp.headers().clone();
        let collected = match upstream_resp.bytes().await {
            Ok(bytes) => bytes,
            Err(err) => {
                tracing::error!(error = %err, "sandbox model proxy: collect upstream body");
                return (
                    StatusCode::BAD_GATEWAY,
                    format!("model proxy collect error: {err}"),
                )
                    .into_response();
            }
        };

        let sse = String::from_utf8_lossy(&collected);
        let calls = parse_function_calls(&sse);
        let local_calls: Vec<&FunctionCall> = calls.iter().filter(|c| is_local(&c.name)).collect();

        if local_calls.is_empty() {
            // No local tool call — either a sandbox function_call or a plain
            // message. Re-emit the collected SSE bytes verbatim (same status +
            // headers/content-type).
            //
            // PARALLEL-CALL EDGE CASE (out of scope for v1): if a single turn
            // contains BOTH a local and a sandbox function call, we take the
            // sub-loop branch below (because a local call is present) and the
            // sandbox call in that turn is effectively dropped. v1 treats "any
            // local call present" as a sub-loop turn.
            let mut builder = Response::builder().status(status.as_u16());
            for (name, value) in resp_headers.iter() {
                match name.as_str() {
                    "content-length" | "transfer-encoding" | "connection" => continue,
                    _ => builder = builder.header(name.clone(), value.clone()),
                }
            }
            return match builder.body(Body::from(collected)) {
                Ok(resp) => resp,
                Err(err) => {
                    tracing::error!(error = %err, "sandbox model proxy response build failed");
                    (
                        StatusCode::INTERNAL_SERVER_ERROR,
                        "model proxy response build failed",
                    )
                        .into_response()
                }
            };
        }

        // Execute each local call via the stub and append its function_call +
        // function_call_output to the request input, then re-query upstream.
        for call in local_calls {
            let output = execute_local_stub(call);
            append_followup(&mut request_body, call, &output);
        }
    }

    (
        StatusCode::INTERNAL_SERVER_ERROR,
        format!("model proxy: local tool sub-loop exceeded {MAX_SUBLOOP_ITERATIONS} iterations"),
    )
        .into_response()
}

/// Returns true if `name` is tagged as a local tool (routed through the
/// sub-loop rather than surfaced to the sandbox agent).
pub fn is_local(name: &str) -> bool {
    name.starts_with(LOCAL_TOOL_PREFIX)
}

/// Inject the fixed stub set of local tools into the Responses request's
/// `tools` array, creating the array if it's missing or not an array.
///
/// For step 2 the local tool set is a single Responses function tool,
/// `local__echo`.
pub fn inject_local_tools(body: &mut Value) {
    let echo = json!({
        "type": "function",
        "name": "local__echo",
        "description": "Echo back the provided text. Local stub tool (step-2 placeholder).",
        "parameters": {
            "type": "object",
            "properties": {
                "text": { "type": "string" }
            },
            "required": ["text"]
        }
    });

    match body.get_mut("tools") {
        Some(Value::Array(tools)) => tools.push(echo),
        _ => {
            body["tools"] = Value::Array(vec![echo]);
        }
    }
}

/// Parse a Responses SSE stream for function-call output items.
///
/// A function call appears as `response.output_item.added` with
/// `item.type == "function_call"` (carrying `name` and `call_id`), followed by
/// `response.function_call_arguments.delta` events whose `delta`s concatenate
/// into the arguments JSON string, and a terminal `response.output_item.done`.
/// Calls are associated by `output_index`, preserving stream order.
pub fn parse_function_calls(sse: &str) -> Vec<FunctionCall> {
    use std::collections::HashMap;

    struct Pending {
        order: usize,
        name: String,
        call_id: String,
        arguments: String,
    }

    // Keyed by output_index; ordered by first-seen via `order`.
    let mut pending: HashMap<i64, Pending> = HashMap::new();
    let mut order: usize = 0;
    let mut finished: Vec<(usize, FunctionCall)> = Vec::new();

    for line in sse.lines() {
        let data = match line.trim_start().strip_prefix("data:") {
            Some(data) => data.trim(),
            None => continue,
        };
        if data.is_empty() || data == "[DONE]" {
            continue;
        }
        let event: Value = match serde_json::from_str(data) {
            Ok(value) => value,
            Err(_) => continue,
        };
        let event_type = event.get("type").and_then(Value::as_str).unwrap_or_default();
        let output_index = event.get("output_index").and_then(Value::as_i64).unwrap_or(-1);

        match event_type {
            "response.output_item.added" => {
                let item = match event.get("item") {
                    Some(item) => item,
                    None => continue,
                };
                if item.get("type").and_then(Value::as_str) != Some("function_call") {
                    continue;
                }
                let name = item.get("name").and_then(Value::as_str).unwrap_or_default();
                let call_id = item.get("call_id").and_then(Value::as_str).unwrap_or_default();
                let arguments = item
                    .get("arguments")
                    .and_then(Value::as_str)
                    .unwrap_or_default();
                pending.insert(
                    output_index,
                    Pending {
                        order,
                        name: name.to_string(),
                        call_id: call_id.to_string(),
                        arguments: arguments.to_string(),
                    },
                );
                order += 1;
            }
            "response.function_call_arguments.delta" => {
                if let Some(entry) = pending.get_mut(&output_index) {
                    if let Some(delta) = event.get("delta").and_then(Value::as_str) {
                        entry.arguments.push_str(delta);
                    }
                }
            }
            "response.output_item.done" => {
                if let Some(mut entry) = pending.remove(&output_index) {
                    // If no deltas were observed, fall back to the final
                    // `arguments` carried on the done item.
                    if entry.arguments.is_empty() {
                        if let Some(args) = event
                            .get("item")
                            .and_then(|item| item.get("arguments"))
                            .and_then(Value::as_str)
                        {
                            entry.arguments = args.to_string();
                        }
                    }
                    finished.push((
                        entry.order,
                        FunctionCall {
                            name: entry.name,
                            call_id: entry.call_id,
                            arguments: entry.arguments,
                        },
                    ));
                }
            }
            _ => {}
        }
    }

    // Include any calls that never received an explicit done event.
    for (_, entry) in pending {
        finished.push((
            entry.order,
            FunctionCall {
                name: entry.name,
                call_id: entry.call_id,
                arguments: entry.arguments,
            },
        ));
    }

    finished.sort_by_key(|(order, _)| *order);
    finished.into_iter().map(|(_, call)| call).collect()
}

/// Execute a local tool via a stub (placeholder for the real local-CLI
/// forwarding in step 3). Returns the tool output as a string suitable for a
/// `function_call_output` item.
pub fn execute_local_stub(call: &FunctionCall) -> String {
    match call.name.as_str() {
        "local__echo" => {
            let text = serde_json::from_str::<Value>(&call.arguments)
                .ok()
                .and_then(|args| {
                    args.get("text")
                        .and_then(Value::as_str)
                        .map(str::to_string)
                })
                .unwrap_or_default();
            json!({ "echo": text }).to_string()
        }
        other => json!({ "error": format!("unknown local tool: {other}") }).to_string(),
    }
}

/// Append the model's function_call and its function_call_output to the
/// Responses request's `input` array, creating it if it's missing or not an
/// array. Arguments are kept as the raw JSON string emitted by the model.
pub fn append_followup(body: &mut Value, call: &FunctionCall, output: &str) {
    let function_call = json!({
        "type": "function_call",
        "call_id": call.call_id,
        "name": call.name,
        "arguments": call.arguments,
    });
    let function_call_output = json!({
        "type": "function_call_output",
        "call_id": call.call_id,
        "output": output,
    });

    match body.get_mut("input") {
        Some(Value::Array(input)) => {
            input.push(function_call);
            input.push(function_call_output);
        }
        _ => {
            body["input"] = Value::Array(vec![function_call, function_call_output]);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn inject_local_tools_creates_array_when_absent() {
        let mut body = json!({ "model": "codex" });
        inject_local_tools(&mut body);

        let tools = body["tools"].as_array().expect("tools array created");
        assert_eq!(tools.len(), 1);
        assert_eq!(tools[0]["type"], "function");
        assert_eq!(tools[0]["name"], "local__echo");
        assert_eq!(tools[0]["parameters"]["required"], json!(["text"]));
        assert_eq!(
            tools[0]["parameters"]["properties"]["text"]["type"],
            "string"
        );
    }

    #[test]
    fn inject_local_tools_appends_to_existing_array() {
        let mut body = json!({
            "tools": [{ "type": "function", "name": "sandbox__shell" }]
        });
        inject_local_tools(&mut body);

        let tools = body["tools"].as_array().expect("tools array");
        assert_eq!(tools.len(), 2);
        assert_eq!(tools[0]["name"], "sandbox__shell");
        assert_eq!(tools[1]["name"], "local__echo");
    }

    #[test]
    fn parse_function_calls_extracts_name_call_id_and_concatenated_arguments() {
        let sse = concat!(
            "event: response.output_item.added\n",
            "data: {\"type\":\"response.output_item.added\",\"output_index\":0,\"item\":{\"type\":\"function_call\",\"id\":\"fc_1\",\"name\":\"local__echo\",\"call_id\":\"call_abc\",\"arguments\":\"\"}}\n",
            "\n",
            "event: response.function_call_arguments.delta\n",
            "data: {\"type\":\"response.function_call_arguments.delta\",\"output_index\":0,\"delta\":\"{\\\"text\\\":\"}\n",
            "\n",
            "event: response.function_call_arguments.delta\n",
            "data: {\"type\":\"response.function_call_arguments.delta\",\"output_index\":0,\"delta\":\"\\\"hi\\\"}\"}\n",
            "\n",
            "event: response.output_item.done\n",
            "data: {\"type\":\"response.output_item.done\",\"output_index\":0,\"item\":{\"type\":\"function_call\",\"id\":\"fc_1\",\"name\":\"local__echo\",\"call_id\":\"call_abc\",\"arguments\":\"{\\\"text\\\":\\\"hi\\\"}\"}}\n",
            "\n",
        );

        let calls = parse_function_calls(sse);
        assert_eq!(calls.len(), 1);
        assert_eq!(calls[0].name, "local__echo");
        assert_eq!(calls[0].call_id, "call_abc");
        assert_eq!(calls[0].arguments, r#"{"text":"hi"}"#);
    }

    #[test]
    fn parse_function_calls_handles_multiple_calls_in_order() {
        let sse = concat!(
            "data: {\"type\":\"response.output_item.added\",\"output_index\":0,\"item\":{\"type\":\"function_call\",\"name\":\"local__echo\",\"call_id\":\"call_0\",\"arguments\":\"\"}}\n",
            "data: {\"type\":\"response.function_call_arguments.delta\",\"output_index\":0,\"delta\":\"{}\"}\n",
            "data: {\"type\":\"response.output_item.added\",\"output_index\":1,\"item\":{\"type\":\"function_call\",\"name\":\"sandbox__shell\",\"call_id\":\"call_1\",\"arguments\":\"\"}}\n",
            "data: {\"type\":\"response.function_call_arguments.delta\",\"output_index\":1,\"delta\":\"{}\"}\n",
            "data: {\"type\":\"response.output_item.done\",\"output_index\":0,\"item\":{}}\n",
            "data: {\"type\":\"response.output_item.done\",\"output_index\":1,\"item\":{}}\n",
        );

        let calls = parse_function_calls(sse);
        assert_eq!(calls.len(), 2);
        assert_eq!(calls[0].call_id, "call_0");
        assert_eq!(calls[1].call_id, "call_1");
    }

    #[test]
    fn is_local_detects_prefix() {
        assert!(is_local("local__echo"));
        assert!(is_local("local__anything"));
        assert!(!is_local("sandbox__shell"));
        assert!(!is_local("echo"));
        assert!(!is_local(""));
    }

    #[test]
    fn execute_local_stub_echoes_text() {
        let call = FunctionCall {
            name: "local__echo".to_string(),
            call_id: "call_abc".to_string(),
            arguments: r#"{"text":"hello world"}"#.to_string(),
        };
        assert_eq!(execute_local_stub(&call), r#"{"echo":"hello world"}"#);
    }

    #[test]
    fn execute_local_stub_unknown_tool_reports_error() {
        let call = FunctionCall {
            name: "local__nope".to_string(),
            call_id: "call_x".to_string(),
            arguments: "{}".to_string(),
        };
        let out: Value = serde_json::from_str(&execute_local_stub(&call)).unwrap();
        assert!(out["error"].as_str().unwrap().contains("local__nope"));
    }

    #[test]
    fn append_followup_appends_two_input_items() {
        let mut body = json!({ "input": [{ "type": "message", "role": "user", "content": "hi" }] });
        let call = FunctionCall {
            name: "local__echo".to_string(),
            call_id: "call_abc".to_string(),
            arguments: r#"{"text":"hi"}"#.to_string(),
        };
        append_followup(&mut body, &call, r#"{"echo":"hi"}"#);

        let input = body["input"].as_array().expect("input array");
        assert_eq!(input.len(), 3);

        let fc = &input[1];
        assert_eq!(fc["type"], "function_call");
        assert_eq!(fc["call_id"], "call_abc");
        assert_eq!(fc["name"], "local__echo");
        assert_eq!(fc["arguments"], r#"{"text":"hi"}"#);

        let fco = &input[2];
        assert_eq!(fco["type"], "function_call_output");
        assert_eq!(fco["call_id"], "call_abc");
        assert_eq!(fco["output"], r#"{"echo":"hi"}"#);
    }

    #[test]
    fn append_followup_creates_input_when_absent() {
        let mut body = json!({ "model": "codex" });
        let call = FunctionCall {
            name: "local__echo".to_string(),
            call_id: "call_1".to_string(),
            arguments: "{}".to_string(),
        };
        append_followup(&mut body, &call, "{}");

        let input = body["input"].as_array().expect("input array created");
        assert_eq!(input.len(), 2);
        assert_eq!(input[0]["type"], "function_call");
        assert_eq!(input[1]["type"], "function_call_output");
    }
}
