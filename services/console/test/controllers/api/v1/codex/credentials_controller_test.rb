require "test_helper"

module Api
  module V1
    module Codex
      class CredentialsControllerTest < ActionDispatch::IntegrationTest
        class StubClient
          def initialize(&block) = (@block = block)
          def refresh(**kwargs) = @block.call(**kwargs)
        end

        def setup
          @principal = Principal.create!(namespace: "default", foreign_id: "slack-user-ucc",
                                         labels: { "managed-by" => "centaur", "slack_user_id" => "U-CC" },
                                         created_by: users(:acme_admin))
          ::Codex::CredentialLinker.refresh_client_factory = -> { live_client }
        end

        def teardown
          ::Codex::CredentialLinker.refresh_client_factory = nil
        end

        def live_client
          StubClient.new do
            Broker::RefreshClient::Result.new(access_token: "AT", refresh_token: "RT2", expires_in: 3600)
          end
        end

        def mint(selector = { "slack_user_id" => "U-CC" })
          CodexPairingToken.create!(principal_selector: selector, created_by: users(:acme_admin))
        end

        def headers(token)
          { "Authorization" => "Bearer #{token}", "Content-Type" => "application/json" }
        end

        def body(refresh_token: "seed-rt", account_id: "acct-9")
          { data: { refresh_token: refresh_token, account_id: account_id } }.to_json
        end

        def json_body = JSON.parse(response.body)

        test "rejects a missing, unknown, expired, or used pairing token" do
          post api_v1_codex_credentials_url, params: body, headers: { "Content-Type" => "application/json" }
          assert_response :unauthorized

          post api_v1_codex_credentials_url, params: body, headers: headers("cpt_unknown")
          assert_response :unauthorized

          used = mint
          used.redeem!
          post api_v1_codex_credentials_url, params: body, headers: headers(used.token)
          assert_response :unauthorized
        end

        test "links the credential, returns 201, and burns the token" do
          token = mint
          assert_difference "BrokerCredential.count", 1 do
            post api_v1_codex_credentials_url, params: body, headers: headers(token.token)
          end
          assert_response :created
          assert_equal "live", json_body.dig("data", "status")

          cred = BrokerCredential.find_by_oid(json_body.dig("data", "credential_id"))
          assert_equal "codex-slack-user-ucc", cred.foreign_id
          assert @principal.grants.joins(:static_secret).exists?
          assert token.reload.used?
        end

        test "a rejected token returns 422 and leaves the pairing token usable" do
          ::Codex::CredentialLinker.refresh_client_factory = lambda {
            StubClient.new { raise Broker::RefreshError.new("bad", stage: "oauth", code: "invalid_grant", retryable: false) }
          }
          token = mint
          post api_v1_codex_credentials_url, params: body, headers: headers(token.token)
          assert_response :unprocessable_entity
          assert_not token.reload.used?
        end

        test "errors when the user has no principal yet" do
          token = mint({ "slack_user_id" => "U-NONE" })
          post api_v1_codex_credentials_url, params: body, headers: headers(token.token)
          assert_response :unprocessable_entity
          assert_match(/direct message/i, json_body.dig("error", "message"))
        end

        test "requires refresh_token and account_id" do
          token = mint
          post api_v1_codex_credentials_url,
               params: { data: { account_id: "a" } }.to_json, headers: headers(token.token)
          assert_response :bad_request
        end
      end
    end
  end
end
