require "test_helper"

class CodexPairingTokenTest < ActiveSupport::TestCase
  def build_token(overrides = {})
    CodexPairingToken.new({
      principal_selector: { "slack_user_id" => "U123" },
      created_by: users(:acme_admin)
    }.merge(overrides))
  end

  test "generates a cpt_ plaintext token and matching hash on create" do
    token = build_token
    assert token.save
    assert token.token.start_with?("cpt_")
    assert_equal Digest::SHA256.hexdigest(token.token), token.token_hash
  end

  test "defaults a short expiry" do
    token = build_token
    token.save!
    assert token.expires_at > Time.current
    assert token.expires_at <= CodexPairingToken::DEFAULT_TTL.from_now + 1.second
  end

  test "requires a non-empty selector" do
    token = build_token(principal_selector: {})
    assert_not token.valid?
    assert_includes token.errors[:principal_selector], "must be a non-empty hash"
  end

  test "find_live_by_token returns a fresh token and nil otherwise" do
    token = build_token
    token.save!
    assert_equal token, CodexPairingToken.find_live_by_token(token.token)
    assert_nil CodexPairingToken.find_live_by_token("cpt_nope")
    assert_nil CodexPairingToken.find_live_by_token(nil)
  end

  test "find_live_by_token rejects expired and used tokens" do
    expired = build_token(expires_at: 1.minute.ago)
    expired.save!
    assert_nil CodexPairingToken.find_live_by_token(expired.token)

    used = build_token
    used.save!
    used.redeem!
    assert_nil CodexPairingToken.find_live_by_token(used.token)
  end

  test "resolve_principal matches a DM principal by its slack_user_id label" do
    principal = Principal.create!(namespace: "default", foreign_id: "slack-user-u123",
                                  labels: { "managed-by" => "centaur", "slack_user_id" => "U123" },
                                  created_by: users(:acme_admin))
    token = build_token
    token.save!
    assert_equal principal, token.resolve_principal
  end

  test "resolve_principal is nil when no principal carries the label yet" do
    token = build_token(principal_selector: { "slack_user_id" => "U-absent" })
    token.save!
    assert_nil token.resolve_principal
  end
end
