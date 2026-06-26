module Api
  module V1
    module Codex
      # Redeems a pairing token to link a user's own ChatGPT/Codex refresh token
      # to their DM principal. Authenticated by the pairing token (NOT an operator
      # API key), so the local helper holds nothing but the short-lived token.
      class CredentialsController < Api::BaseController
        skip_before_action :authenticate_api_key!
        before_action :authenticate_pairing_token!

        # POST /api/v1/codex/credentials
        # body: { data: { refresh_token:, account_id: } }
        def create
          principal = @pairing_token.resolve_principal
          if principal.nil?
            return render_error(status: :unprocessable_entity,
              message: "No conversation found for you yet. Send the bot a direct message first, then re-run connect.")
          end

          refresh_token = data_params[:refresh_token].presence
          account_id = data_params[:account_id].presence
          raise ActionController::ParameterMissing, :refresh_token if refresh_token.blank?
          raise ActionController::ParameterMissing, :account_id if account_id.blank?

          result = ::Codex::CredentialLinker.new(
            principal: principal, created_by: @pairing_token.created_by
          ).link!(refresh_token: refresh_token, account_id: account_id)

          # Leave the token usable on a bad seed so the user can `codex login`
          # again and retry; only burn it once a credential is live.
          unless result.live
            return render_error(status: :unprocessable_entity,
              message: "Could not validate your Codex token (#{result.credential.dead_reason}). " \
                       "Run `codex login` again, then re-run connect.")
          end

          @pairing_token.redeem!
          render status: :created, json: { data: {
            principal_id: principal.oid,
            credential_id: result.credential.oid,
            status: result.credential.status
          } }
        rescue ActiveRecord::RecordInvalid => e
          render_validation_error(e.record)
        end

        private

        def authenticate_pairing_token!
          token = bearer_token
          @pairing_token = CodexPairingToken.find_live_by_token(token) if token.present?
          return if @pairing_token

          render_error(status: :unauthorized, message: "invalid, expired, or used pairing token")
        end

        # Grants/credentials are attributed to whoever minted the token (the bot
        # service account), since the linking user is not a console operator.
        def current_user
          @pairing_token&.created_by
        end
      end
    end
  end
end
