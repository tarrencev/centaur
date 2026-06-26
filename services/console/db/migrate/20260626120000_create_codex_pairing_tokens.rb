class CreateCodexPairingTokens < ActiveRecord::Migration[8.1]
  def change
    create_table :codex_pairing_tokens do |t|
      # SHA-256 of the plaintext token; the plaintext is shown once at mint and
      # never stored, mirroring ApiKey.
      t.string :token_hash, null: false
      # Label match that resolves the target DM principal at upload time, e.g.
      # {"slack_user_id" => "U123", "slack_team_id" => "T456"}. Stored instead of
      # a derived foreign_id so we never re-implement Rust derive_principal.
      t.jsonb :principal_selector, null: false, default: {}
      # Short-lived and single-use: an unused token past expires_at is dead, and
      # used_at is stamped the first time it mints a credential.
      t.datetime :expires_at, null: false
      t.datetime :used_at
      t.bigint :created_by_id, null: false

      t.timestamps
    end

    add_index :codex_pairing_tokens, :token_hash, unique: true
    add_index :codex_pairing_tokens, :created_by_id
    add_index :codex_pairing_tokens, :expires_at
  end
end
