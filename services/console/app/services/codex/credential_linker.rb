module Codex
  # Links a user's own ChatGPT/Codex subscription token to their DM principal.
  #
  # Upserts a per-principal standalone BrokerCredential seeded with the user's
  # refresh token, plus two wrapping static secrets granted DIRECTLY to the
  # principal (priority 100), which override the shared `openai-codex` infra-role
  # grant (priority 0) via Principal#suppressed_conflict_credentials:
  #   1. token_broker -> Authorization: Bearer <live access token>  (chatgpt.com)
  #   2. control_plane -> chatgpt-account-id: <account id>          (chatgpt.com)
  #
  # The credential is keyed by principal (foreign_id "codex-<principal fid>"), so
  # re-linking — even with a different ChatGPT account — overwrites the single
  # credential rather than accumulating stale grants.
  #
  # SECURITY: never logs the refresh/access token. The refresh loop
  # (Broker::PollRefreshJob -> BrokerCredential#refresh!) keeps the access token
  # live so the agent works while the user's machine is offline.
  class CredentialLinker
    # OpenAI Codex CLI's public ChatGPT OAuth client (verified against openai/codex
    # codex-rs/login). Public PKCE client: no client_secret, no scope on refresh.
    TOKEN_ENDPOINT = "https://auth.openai.com/oauth/token".freeze
    CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann".freeze
    # Egress host for ChatGPT-subscription Codex (chatgpt.com/backend-api/codex).
    API_HOST = "chatgpt.com".freeze

    Result = Data.define(:credential, :live)

    # Test seam, mirroring FlowsController.exchange_client_factory: when set,
    # supplies the refresh client the validation mint uses. nil in production, so
    # the credential builds its own Broker::RefreshClient.
    class_attribute :refresh_client_factory, default: nil

    # refresh_client is an explicit per-call override (tests); otherwise the class
    # factory (if any) is consulted, else the credential builds its own.
    def initialize(principal:, created_by:, refresh_client: nil)
      @principal = principal
      @created_by = created_by
      @refresh_client = refresh_client || self.class.refresh_client_factory&.call
    end

    # Upserts the credential + secrets + grants, then validates the seed by
    # minting once. Returns a Result; live is false when the refresh token is
    # rejected (the caller surfaces a re-link prompt).
    def link!(refresh_token:, account_id:)
      credential = nil
      ActiveRecord::Base.transaction do
        credential = upsert_credential(refresh_token)
        grant!(upsert_token_secret(credential))
        grant!(upsert_account_secret(account_id))
      end

      # Validate + bootstrap outside the transaction: refresh! makes a network
      # call under its own row lock and persists the outcome (dead on a bad seed).
      credential.refresh_client = @refresh_client if @refresh_client
      credential.refresh!
      credential.reload
      Result.new(credential: credential, live: !credential.dead?)
    end

    private

    attr_reader :principal, :created_by

    def upsert_credential(refresh_token)
      credential = BrokerCredential.find_or_initialize_by(
        namespace: principal.namespace,
        foreign_id: "codex-#{principal.foreign_id}"
      )
      credential.assign_attributes(
        name: "Codex – #{principal_label}",
        token_endpoint: TOKEN_ENDPOINT,
        client_id: CLIENT_ID,
        scopes: [], # Codex sends no scope on refresh.
        # A fresh seed re-bootstraps: reset rotation/dead state and make it due now.
        refresh_token: refresh_token,
        access_token: nil,
        expires_at: nil,
        last_refresh: nil,
        dead: false,
        dead_reason: nil,
        failure_count: 0,
        next_attempt_at: Time.current
      )
      credential.created_by ||= created_by
      credential.save!
      credential
    end

    def upsert_token_secret(credential)
      secret = StaticSecret.find_or_initialize_by(broker_credential: credential)
      secret.namespace = principal.namespace
      secret.name = "Codex token – #{principal_label}"
      secret.inject_config = { "header" => "Authorization", "formatter" => "Bearer {{ .Value }}" }
      secret.created_by ||= created_by
      secret.save!
      upsert_source(secret, source_type: "token_broker", config: { "credential_id" => credential.oid })
      ensure_rule(secret)
      secret
    end

    def upsert_account_secret(account_id)
      secret = StaticSecret.find_or_initialize_by(
        namespace: principal.namespace,
        foreign_id: "codex-account-#{principal.foreign_id}"
      )
      secret.name = "Codex account id – #{principal_label}"
      secret.inject_config = { "header" => "chatgpt-account-id" }
      secret.created_by ||= created_by
      secret.save!
      upsert_source(secret, source_type: "control_plane", value: account_id)
      ensure_rule(secret)
      secret
    end

    def upsert_source(secret, source_type:, config: {}, value: nil)
      source = secret.source || secret.build_source
      source.source_type = source_type if source.new_record? # source_type is readonly
      source.config = config
      source.secret = value if source_type == "control_plane"
      source.save!
    end

    def ensure_rule(secret)
      return if secret.rules.any? { |rule| rule.host == API_HOST }
      secret.rules.create!(host: API_HOST, http_methods: [], paths: [], position: 0)
    end

    def grant!(secret)
      principal.grants.create_with(created_by: created_by).find_or_create_by!(static_secret: secret)
    rescue ActiveRecord::RecordNotUnique
      # A concurrent identical grant won the unique index; it is already present.
    end

    def principal_label
      principal.name.presence || principal.foreign_id
    end
  end
end
