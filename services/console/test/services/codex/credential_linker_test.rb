require "test_helper"

module Codex
  class CredentialLinkerTest < ActiveSupport::TestCase
    # Mirrors BrokerCredentialTest::StubClient: a fixed refresh outcome with no network.
    class StubClient
      def initialize(&block) = (@block = block)
      def refresh(**kwargs) = @block.call(**kwargs)
    end

    def live_client(access_token: "AT-live")
      StubClient.new do
        Broker::RefreshClient::Result.new(access_token: access_token, refresh_token: "RT-2", expires_in: 3600)
      end
    end

    def dead_client
      StubClient.new { raise Broker::RefreshError.new("bad seed", stage: "oauth", code: "invalid_grant", retryable: false) }
    end

    def principal
      @principal ||= Principal.create!(namespace: "default", foreign_id: "slack-user-u777",
                                       labels: { "managed-by" => "centaur", "slack_user_id" => "U777" },
                                       created_by: users(:acme_admin))
    end

    def link!(refresh_client:, account_id: "acct-1", refresh_token: "seed-rt")
      CredentialLinker.new(principal: principal, created_by: users(:acme_admin), refresh_client: refresh_client)
                      .link!(refresh_token: refresh_token, account_id: account_id)
    end

    test "creates a per-principal codex broker credential keyed by principal" do
      result = link!(refresh_client: live_client)
      cred = result.credential
      assert result.live
      assert_equal "codex-slack-user-u777", cred.foreign_id
      assert_equal "default", cred.namespace
      assert_equal CredentialLinker::CLIENT_ID, cred.client_id
      assert_equal CredentialLinker::TOKEN_ENDPOINT, cred.token_endpoint
      assert_equal [], cred.scopes
      assert_nil cred.oauth_app_id
    end

    test "creates a token_broker secret injecting Authorization on chatgpt.com, granted directly" do
      cred = link!(refresh_client: live_client).credential
      secret = StaticSecret.find_by(broker_credential: cred)
      assert_equal "token_broker", secret.source.source_type
      assert_equal cred.oid, secret.source.config["credential_id"]
      assert_equal({ "header" => "Authorization", "formatter" => "Bearer {{ .Value }}" }, secret.inject_config)
      assert_equal [ "chatgpt.com" ], secret.rules.map(&:host)
      assert_equal [ "header:authorization" ], secret.proxy_conflict_targets

      grant = principal.grants.find_by(static_secret: secret)
      assert_equal Grant::DEFAULT_DIRECT_PRIORITY, grant.priority
    end

    test "creates a control_plane secret injecting the account id, granted directly" do
      link!(refresh_client: live_client, account_id: "acct-xyz")
      secret = StaticSecret.find_by(namespace: "default", foreign_id: "codex-account-slack-user-u777")
      assert_equal "control_plane", secret.source.source_type
      assert_equal "acct-xyz", secret.source.secret
      assert_equal({ "header" => "chatgpt-account-id" }, secret.inject_config)
      assert_equal [ "header:chatgpt-account-id" ], secret.proxy_conflict_targets
      assert principal.grants.exists?(static_secret: secret)
    end

    test "re-linking is idempotent and updates the rotated account id" do
      link!(refresh_client: live_client, account_id: "acct-1", refresh_token: "seed-1")

      assert_no_difference [ "BrokerCredential.count", "StaticSecret.count", "Grant.count" ] do
        link!(refresh_client: live_client, account_id: "acct-2", refresh_token: "seed-2")
      end

      account_secret = StaticSecret.find_by(foreign_id: "codex-account-slack-user-u777")
      assert_equal "acct-2", account_secret.source.secret
    end

    test "a rejected refresh token leaves the credential dead and not live" do
      result = link!(refresh_client: dead_client)
      assert_not result.live
      assert result.credential.dead?
      # The wrapping secret exists but is non-deliverable until a good token mints.
      secret = StaticSecret.find_by(broker_credential: result.credential)
      assert_not secret.source.deliverable?
    end
  end
end
