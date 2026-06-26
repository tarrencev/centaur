require "test_helper"

module Api
  module V1
    module Codex
      class PairingTokensControllerTest < ActionDispatch::IntegrationTest
        ACME_TOKEN = "iak_acme-ci-token".freeze

        def auth_headers(token = ACME_TOKEN)
          { "Authorization" => "Bearer #{token}", "Content-Type" => "application/json" }
        end

        def json_body = JSON.parse(response.body)

        test "requires an operator API key" do
          post api_v1_codex_pairing_tokens_url, params: { data: { slack_user_id: "U1" } }.to_json,
               headers: { "Content-Type" => "application/json" }
          assert_response :unauthorized
        end

        test "mints a single-use token and reports principal_found false when the user is unknown" do
          assert_difference "CodexPairingToken.count", 1 do
            post api_v1_codex_pairing_tokens_url,
                 params: { data: { platform: "slack", slack_user_id: "U-new" } }.to_json,
                 headers: auth_headers
          end
          assert_response :created
          assert json_body.dig("data", "token").start_with?("cpt_")
          assert_equal false, json_body.dig("data", "principal_found")

          token = CodexPairingToken.find_by_oid(json_body.dig("data", "id"))
          assert_equal({ "slack_user_id" => "U-new" }, token.principal_selector)
          assert_equal users(:acme_admin), token.created_by
        end

        test "folds the team id into the selector and reports principal_found when matched" do
          Principal.create!(namespace: "default", foreign_id: "slack-user-team-u9",
                            labels: { "managed-by" => "centaur", "slack_user_id" => "U9", "slack_team_id" => "T9" },
                            created_by: users(:acme_admin))
          post api_v1_codex_pairing_tokens_url,
               params: { data: { slack_user_id: "U9", slack_team_id: "T9" } }.to_json,
               headers: auth_headers
          assert_response :created
          assert_equal true, json_body.dig("data", "principal_found")
        end

        test "rejects a missing slack_user_id" do
          post api_v1_codex_pairing_tokens_url, params: { data: { platform: "slack" } }.to_json,
               headers: auth_headers
          assert_response :bad_request
        end
      end
    end
  end
end
