module Api
  module V1
    module Codex
      # Mints a single-use pairing token bound to a DM principal selector, so a
      # user's local `centaur codex link` helper can upload their Codex refresh
      # token without an operator API key. Called by the `/connect-codex` bot
      # command (operator ApiKey auth).
      class PairingTokensController < Api::BaseController
        # POST /api/v1/codex/pairing_tokens
        # body: { data: { platform: "slack", slack_user_id:, slack_team_id? } }
        def create
          selector = build_selector
          token = CodexPairingToken.create!(principal_selector: selector, created_by: current_user)
          render status: :created, json: { data: {
            id: token.oid,
            token: token.token, # plaintext, shown once
            expires_at: token.expires_at,
            # Lets the bot warn first-timers who have not messaged it yet.
            principal_found: token.resolve_principal.present?
          } }
        rescue ActiveRecord::RecordInvalid => e
          render_validation_error(e.record)
        end

        private

        def build_selector
          platform = data_params[:platform].presence || "slack"
          raise ActionController::BadRequest, "unsupported platform #{platform.inspect}" unless platform == "slack"

          user_id = data_params[:slack_user_id].presence
          raise ActionController::ParameterMissing, :slack_user_id if user_id.blank?

          selector = { "slack_user_id" => user_id }
          team = data_params[:slack_team_id].presence
          selector["slack_team_id"] = team if team
          selector
        end
      end
    end
  end
end
