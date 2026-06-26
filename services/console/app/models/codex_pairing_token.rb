# A short-lived, single-use token that lets a user's local `centaur codex link`
# helper upload their ChatGPT/Codex refresh token without an operator API key.
#
# The token carries (server-side, untamperable) the label selector that resolves
# which DM principal the uploaded credential is granted to, so the helper needs
# no trust beyond holding the token. Minted by the `/connect-codex` bot command
# (operator ApiKey auth); redeemed once by the upload endpoint.
#
# Token material is stored hash-only, exactly like ApiKey: the plaintext is
# returned once at mint and never persisted.
class CodexPairingToken < ApplicationRecord
  oid_prefix "cpt"

  TOKEN_PREFIX = "cpt_".freeze
  TOKEN_FORMAT = /\Acpt_[0-9a-f]{64}\z/
  DEFAULT_TTL = 15.minutes

  attr_readonly :token_hash, :principal_selector
  attr_accessor :token

  belongs_to :created_by, class_name: "User"

  validates :token_hash, presence: true, uniqueness: true
  validates :expires_at, presence: true
  validate :token_matches_format, on: :create
  validate :selector_is_a_hash

  before_validation :issue_token, on: :create
  before_validation :apply_default_expiry, on: :create

  def self.hash_token(plaintext)
    Digest::SHA256.hexdigest(plaintext)
  end

  # Resolves a live (unexpired, unused) token from its plaintext, or nil.
  def self.find_live_by_token(plaintext, now: Time.current)
    return nil if plaintext.blank?
    token = find_by(token_hash: hash_token(plaintext))
    return nil if token.nil? || token.expired?(now: now) || token.used?
    token
  end

  def expired?(now: Time.current)
    expires_at.present? && expires_at <= now
  end

  def used?
    used_at.present?
  end

  def redeem!(at: Time.current)
    update!(used_at: at)
  end

  # The DM principal this token binds to, matched by its raw Slack labels
  # (e.g. {"slack_user_id" => "U123"}). Only DM principals carry slack_user_id,
  # so the match is unique; nil until the user has messaged the bot at least once.
  def resolve_principal
    return nil unless principal_selector.is_a?(Hash) && principal_selector.present?
    Principal.where("labels @> ?", principal_selector.to_json).order(:id).first
  end

  private

  def issue_token
    return if token_hash.present?
    self.token = "#{TOKEN_PREFIX}#{SecureRandom.hex(32)}"
    self.token_hash = self.class.hash_token(token)
  end

  def apply_default_expiry
    self.expires_at ||= DEFAULT_TTL.from_now
  end

  def token_matches_format
    return if token.blank?
    return if token.match?(TOKEN_FORMAT)
    errors.add(:token, "must match #{TOKEN_FORMAT.inspect} (cpt_ + 32-byte lowercase hex)")
  end

  def selector_is_a_hash
    errors.add(:principal_selector, "must be a non-empty hash") unless principal_selector.is_a?(Hash) && principal_selector.present?
  end
end
