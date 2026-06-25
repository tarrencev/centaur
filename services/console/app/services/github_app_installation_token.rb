require "base64"
require "digest"
require "json"
require "net/http"
require "openssl"
require "uri"

class GithubAppInstallationToken
  DEFAULT_API_URL = "https://api.github.com".freeze
  DEFAULT_EARLY_REFRESH_SECONDS = 15.minutes.to_i

  Token = Struct.new(:value, :expires_at, keyword_init: true)

  class << self
    attr_writer :http_client

    def fetch(config, now: Time.current)
      cfg = normalized_config(config)
      cached = cached_token(cfg, now)
      return cached.value if cached

      @mutex.synchronize do
        cached = cached_token(cfg, now)
        return cached.value if cached

        token = mint(cfg, now, cached: cached_token(cfg, now, early_refresh: false))
        @cache[cache_key(cfg)] = token if token
        token&.value
      end
    rescue => e
      Rails.logger.warn { "github_app token source failed: #{e.class}: #{e.message}" }
      nil
    end

    def reset_cache!
      @mutex.synchronize { @cache.clear }
    end

    private

    def normalized_config(config)
      config ||= {}
      api_url = config["api_url"].presence || DEFAULT_API_URL
      {
        "api_url" => api_url.to_s.sub(%r{/+\z}, ""),
        "app_id" => env_value(config.fetch("app_id_env")),
        "installation_id" => env_value(config.fetch("installation_id_env")),
        "private_key_pem" => private_key_pem(env_value(config.fetch("private_key_b64_env"))),
        "early_refresh_seconds" => Integer(config["early_refresh_seconds"].presence || DEFAULT_EARLY_REFRESH_SECONDS)
      }
    end

    def env_value(name)
      value = ENV[name.to_s].to_s.strip
      raise ArgumentError, "missing GitHub App credential env #{name}" if value.blank?
      value
    end

    def private_key_pem(encoded)
      Base64.strict_decode64(encoded)
    rescue ArgumentError
      raise ArgumentError, "GitHub App private key env must be base64-encoded PEM"
    end

    def cached_token(cfg, now, early_refresh: true)
      token = @cache[cache_key(cfg)]
      return nil unless token
      threshold = early_refresh ? now + cfg["early_refresh_seconds"] : now
      return nil unless token.expires_at > threshold
      token
    end

    def cache_key(cfg)
      [
        cfg["api_url"],
        cfg["app_id"],
        cfg["installation_id"],
        Digest::SHA256.hexdigest(cfg["private_key_pem"])
      ].join(":")
    end

    def mint(cfg, now, cached: nil)
      uri = URI("#{cfg["api_url"]}/app/installations/#{cfg["installation_id"]}/access_tokens")
      req = Net::HTTP::Post.new(uri)
      req["Authorization"] = "Bearer #{jwt(cfg, now)}"
      req["Accept"] = "application/vnd.github+json"
      req["X-GitHub-Api-Version"] = "2022-11-28"
      res = http.request(uri, req)
      unless res.is_a?(Net::HTTPSuccess)
        return cached if cached
        raise "GitHub installation token mint failed: status=#{res.code}"
      end
      body = JSON.parse(res.body)
      value = body["token"].to_s
      raise "GitHub installation token response did not include a ghs_ token" unless value.start_with?("ghs_")
      Token.new(value: value, expires_at: Time.iso8601(body.fetch("expires_at")))
    rescue
      return cached if cached
      raise
    end

    def jwt(cfg, now)
      header = base64url(JSON.generate({ alg: "RS256", typ: "JWT" }))
      payload = base64url(JSON.generate({
        iat: now.to_i - 60,
        exp: now.to_i + 9.minutes.to_i,
        iss: cfg["app_id"]
      }))
      signing_input = "#{header}.#{payload}"
      key = OpenSSL::PKey::RSA.new(cfg["private_key_pem"])
      signature = key.sign(OpenSSL::Digest.new("SHA256"), signing_input)
      "#{signing_input}.#{base64url(signature)}"
    end

    def base64url(value)
      Base64.urlsafe_encode64(value, padding: false)
    end

    def http
      @http_client ||= Net::HTTP
    end
  end

  @cache = {}
  @mutex = Mutex.new
end
