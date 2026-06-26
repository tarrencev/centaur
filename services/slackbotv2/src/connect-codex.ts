import type { Message } from 'chat'

import { serializeMessage, slackConversationId, slackConversationKind } from './session-api'
import type { SlackbotV2Fetch, SlackbotV2Options } from './types'

// `/connect-codex` lets a user link their own ChatGPT/Codex subscription so
// agents in their DM run on their token instead of the shared key. The bot mints
// a single-use pairing token on centaur-console and DMs the user the steps; their
// local `codex-link` helper does the upload. DM-only (a DM principal keys on the
// user); channel sessions keep the shared credential.

const COMMAND = 'connect-codex'
const PAIRING_PATH = '/api/v1/codex/pairing_tokens'
const REQUEST_TIMEOUT_MS = 10_000

// Matches a bare `connect-codex` (optionally a leading slash), case-insensitive.
export function isConnectCodexCommand(text: string | undefined): boolean {
  if (!text) return false
  return text.trim().replace(/^\//, '').toLowerCase() === COMMAND
}

export type CodexPairingResult = { ok: boolean; message: string }

// Minimal reply surface so the handler is decoupled from the chat lib's Thread
// generics (and trivially testable).
export type CodexReplyThread = { post(text: string): Promise<unknown> }

// Mints a pairing token on the console and returns the user-facing reply text.
// Exported for direct testing with a stub fetch.
export async function requestCodexPairing(params: {
  options: SlackbotV2Options
  slackUserId: string
  slackTeamId?: string
  fetchFn: SlackbotV2Fetch
}): Promise<CodexPairingResult> {
  const { options, slackUserId, slackTeamId, fetchFn } = params

  if (!options.consoleUrl || !options.consoleApiKey) {
    return {
      ok: false,
      message: 'Codex linking is not configured yet. Ask an admin to set the console URL and API key.'
    }
  }

  const url = options.consoleUrl.replace(/\/+$/, '') + PAIRING_PATH
  const data: Record<string, string> = { platform: 'slack', slack_user_id: slackUserId }
  if (slackTeamId) data.slack_team_id = slackTeamId

  let response: Response
  try {
    response = await withTimeout(
      fetchFn,
      url,
      {
        method: 'POST',
        headers: {
          'content-type': 'application/json',
          authorization: `Bearer ${options.consoleApiKey}`
        },
        body: JSON.stringify({ data })
      },
      REQUEST_TIMEOUT_MS
    )
  } catch {
    return { ok: false, message: 'Could not reach centaur to start linking. Try again in a moment.' }
  }

  if (!response.ok) {
    return {
      ok: false,
      message: 'Could not start Codex linking (the console rejected the request). Ask an admin to check the logs.'
    }
  }

  const payload = (await response.json().catch(() => ({}))) as {
    data?: { token?: string; principal_found?: boolean }
  }
  const token = payload.data?.token
  if (!token) {
    return { ok: false, message: 'Linking response was malformed. Ask an admin to check the console.' }
  }

  const publicUrl = options.consolePublicUrl ?? options.consoleUrl
  const firstTimeNote =
    payload.data?.principal_found === false
      ? '\n\n_Note: I don’t see a conversation from you yet. If linking says "send a direct message first", just message me once, then re-run the helper with the same token._'
      : ''

  return { ok: true, message: instructions(token, publicUrl) + firstTimeNote }
}

// Entry point invoked from the message dispatch in index.ts.
export async function handleConnectCodexCommand(
  thread: CodexReplyThread,
  message: Message,
  options: SlackbotV2Options
): Promise<void> {
  const serialized = await serializeMessage(message)
  if (slackConversationKind(slackConversationId(serialized)) !== 'dm') {
    await thread.post(
      'Send me a *direct message* with `connect-codex` to link your Codex account — per-user linking only works in DMs.'
    )
    return
  }

  const slackUserId = message.author.userId
  if (!slackUserId) {
    await thread.post('I couldn’t determine your Slack user id, so I can’t link Codex. Please try again.')
    return
  }

  const result = await requestCodexPairing({
    options,
    slackUserId,
    slackTeamId: serialized.teamId || undefined,
    fetchFn: options.fetch ?? fetch
  })
  await thread.post(result.message)
}

function instructions(token: string, consoleUrl: string): string {
  return [
    '*Link your ChatGPT/Codex subscription* so I run on your account:',
    '',
    '1. Run `codex login` on your machine (skip if already signed in).',
    '2. Run this — the token is single-use and expires in 15 minutes:',
    '```',
    `codex-link ${token} --console-url ${consoleUrl}`,
    '```',
    'That’s it. Your machine can go offline afterwards — I keep the token refreshed.'
  ].join('\n')
}

async function withTimeout(
  fetchFn: SlackbotV2Fetch,
  url: string,
  init: RequestInit,
  timeoutMs: number
): Promise<Response> {
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), timeoutMs)
  try {
    return await fetchFn(url, { ...init, signal: controller.signal })
  } finally {
    clearTimeout(timer)
  }
}
