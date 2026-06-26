import { describe, expect, test } from 'bun:test'
import { isConnectCodexCommand, requestCodexPairing } from '../src/connect-codex'
import type { SlackbotV2Options } from '../src/types'

type RecordedRequest = { url: string; body: unknown; headers: HeadersInit | undefined }

function fakeConsole(response: { status: number; body?: unknown }) {
  const requests: RecordedRequest[] = []
  const fetchFn = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    requests.push({
      url: String(input),
      body: init?.body ? JSON.parse(String(init.body)) : undefined,
      headers: init?.headers
    })
    return Response.json(response.body ?? {}, { status: response.status })
  }
  return { fetchFn, requests }
}

function options(overrides: Partial<SlackbotV2Options> = {}): SlackbotV2Options {
  return {
    apiUrl: 'http://api.test',
    botToken: 'xoxb-test',
    signingSecret: 'secret',
    consoleUrl: 'http://console.test',
    consoleApiKey: 'iak_console',
    ...overrides
  }
}

describe('isConnectCodexCommand', () => {
  test('matches the bare command, a slash form, and trims/casefolds', () => {
    expect(isConnectCodexCommand('connect-codex')).toBe(true)
    expect(isConnectCodexCommand('  /Connect-Codex  ')).toBe(true)
  })

  test('does not match other text', () => {
    expect(isConnectCodexCommand('connect codex now')).toBe(false)
    expect(isConnectCodexCommand('hello')).toBe(false)
    expect(isConnectCodexCommand(undefined)).toBe(false)
  })
})

describe('requestCodexPairing', () => {
  test('posts the selector to the console with the operator bearer key', async () => {
    const { fetchFn, requests } = fakeConsole({ status: 201, body: { data: { token: 'cpt_abc', principal_found: true } } })
    const result = await requestCodexPairing({
      options: options(),
      slackUserId: 'U9',
      slackTeamId: 'T9',
      fetchFn
    })

    expect(result.ok).toBe(true)
    expect(result.message).toContain('cpt_abc')
    const req = requests[0]
    expect(req.url).toBe('http://console.test/api/v1/codex/pairing_tokens')
    expect(req.body).toEqual({ data: { platform: 'slack', slack_user_id: 'U9', slack_team_id: 'T9' } })
    expect((req.headers as Record<string, string>).authorization).toBe('Bearer iak_console')
  })

  test('uses the public console url in the instructions when set', async () => {
    const { fetchFn } = fakeConsole({ status: 201, body: { data: { token: 'cpt_xyz', principal_found: true } } })
    const result = await requestCodexPairing({
      options: options({ consolePublicUrl: 'https://console.example.com' }),
      slackUserId: 'U9',
      fetchFn
    })
    expect(result.message).toContain('--console-url https://console.example.com')
  })

  test('adds a first-time hint when the user has no principal yet', async () => {
    const { fetchFn } = fakeConsole({ status: 201, body: { data: { token: 'cpt_abc', principal_found: false } } })
    const result = await requestCodexPairing({ options: options(), slackUserId: 'U9', fetchFn })
    expect(result.message).toContain('direct message')
  })

  test('reports a friendly error when linking is not configured', async () => {
    const { fetchFn } = fakeConsole({ status: 201 })
    const result = await requestCodexPairing({
      options: options({ consoleApiKey: undefined }),
      slackUserId: 'U9',
      fetchFn
    })
    expect(result.ok).toBe(false)
    expect(result.message).toContain('not configured')
  })

  test('reports an error when the console rejects the request', async () => {
    const { fetchFn } = fakeConsole({ status: 401 })
    const result = await requestCodexPairing({ options: options(), slackUserId: 'U9', fetchFn })
    expect(result.ok).toBe(false)
  })
})
