require "test_helper"

# Proves the load-bearing claim of per-user Codex auth WITHOUT any proxy, k8s,
# Slack, or OpenAI: a linked user's per-user Codex secrets (direct grant,
# priority 100) override the shared `openai-codex` credential (infra-role grant,
# priority 0) in the exact config the proxy-sync endpoint serves
# (Principal#effective_config -> served_credentials -> suppressed_conflict_credentials).
class CodexPrioritySuppressionTest < ActionDispatch::IntegrationTest
  class StubClient
    def initialize(&block) = (@block = block)
    def refresh(**kwargs) = @block.call(**kwargs)
  end

  def setup
    @admin = users(:acme_admin)

    # The DM principal a Slack 1:1 session binds the proxy to.
    @principal = Principal.create!(namespace: "default", foreign_id: "slack-user-udm",
                                   labels: { "managed-by" => "centaur", "slack_user_id" => "U-DM" },
                                   created_by: @admin)

    # The shared deployment baseline: an `infra` role the principal holds,
    # carrying the global `openai-codex` token + global account-id, exactly as
    # api-rs registers them (role grants, priority 0).
    @infra = Role.create!(namespace: "default", foreign_id: "infra", created_by: @admin)
    PrincipalRole.create!(principal: @principal, role: @infra)
    grant_shared_codex_token!
    grant_shared_account_id!
  end

  test "a linked user's Codex token and account id override the shared ones on chatgpt.com" do
    # Link the user (per-user direct grants, priority 100). A live stub mints an
    # access token so the wrapping token_broker secret is deliverable.
    Codex::CredentialLinker.new(
      principal: @principal,
      created_by: @admin,
      refresh_client: StubClient.new {
        Broker::RefreshClient::Result.new(access_token: "PER-USER-TOKEN", refresh_token: "RT", expires_in: 3600)
      }
    ).link!(refresh_token: "seed", account_id: "PER-USER-ACCOUNT")

    secrets = @principal.effective_config(redact_secrets: false)["secrets"]

    # Exactly one Authorization injector survives for chatgpt.com, and it carries
    # the per-user token — the shared openai-codex one was suppressed.
    authz = secrets.select { |s| header(s) == "Authorization" && hosts(s).include?("chatgpt.com") }
    assert_equal 1, authz.size, "expected the shared Codex token to be suppressed"
    assert_equal "PER-USER-TOKEN", authz.first.dig("source", "value")

    # Same for the chatgpt-account-id header.
    account = secrets.select { |s| header(s) == "chatgpt-account-id" && hosts(s).include?("chatgpt.com") }
    assert_equal 1, account.size, "expected the shared account id to be suppressed"
    assert_equal "PER-USER-ACCOUNT", account.first.dig("source", "value")
  end

  test "an unlinked user still resolves to the shared Codex credential" do
    secrets = @principal.effective_config(redact_secrets: false)["secrets"]
    authz = secrets.select { |s| header(s) == "Authorization" && hosts(s).include?("chatgpt.com") }
    assert_equal 1, authz.size
    assert_equal "SHARED-TOKEN", authz.first.dig("source", "value")
  end

  private

  def header(serialized_secret) = serialized_secret.dig("inject", "header")
  def hosts(serialized_secret) = Array(serialized_secret["rules"]).map { |r| r["host"] }

  # Mirrors the shared `openai-codex` broker credential + its grantable wrapping
  # secret, granted to the infra role. Given a live access token so it competes
  # (a non-deliverable secret would be dropped, not suppressed).
  def grant_shared_codex_token!
    credential = BrokerCredential.create!(namespace: "default", foreign_id: "openai-codex",
                                          token_endpoint: "https://auth.openai.com/oauth/token",
                                          client_id: "shared", refresh_token: "shared-rt",
                                          access_token: "SHARED-TOKEN", expires_at: 1.hour.from_now,
                                          last_refresh: Time.current, created_by: @admin)
    secret = StaticSecret.create!(namespace: "default", foreign_id: "shared-codex-token",
                                  inject_config: { "header" => "Authorization", "formatter" => "Bearer {{ .Value }}" },
                                  created_by: @admin)
    SecretSource.create!(static_secret: secret, source_type: "token_broker",
                         config: { "credential_id" => credential.oid })
    RequestRule.create!(static_secret: secret, host: "chatgpt.com", http_methods: [], paths: [], position: 0)
    Grant.create!(role: @infra, static_secret: secret, created_by: @admin)
  end

  # Mirrors the global OPENAI_CODEX_ACCOUNT_ID placeholder injection (env source),
  # granted to the infra role.
  def grant_shared_account_id!
    secret = StaticSecret.create!(namespace: "default", foreign_id: "shared-codex-account",
                                  inject_config: { "header" => "chatgpt-account-id" },
                                  created_by: @admin)
    SecretSource.create!(static_secret: secret, source_type: "control_plane", secret: "SHARED-ACCOUNT")
    RequestRule.create!(static_secret: secret, host: "chatgpt.com", http_methods: [], paths: [], position: 0)
    Grant.create!(role: @infra, static_secret: secret, created_by: @admin)
  end
end
