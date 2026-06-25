require "test_helper"

class GithubAppInstallationTokenTest < ActiveSupport::TestCase
  setup do
    GithubAppInstallationToken.reset_cache!
    @env = {
      "TEST_GITHUB_APP_ID" => ENV["TEST_GITHUB_APP_ID"],
      "TEST_GITHUB_INSTALLATION_ID" => ENV["TEST_GITHUB_INSTALLATION_ID"],
      "TEST_GITHUB_PRIVATE_KEY_B64" => ENV["TEST_GITHUB_PRIVATE_KEY_B64"]
    }
  end

  teardown do
    GithubAppInstallationToken.reset_cache!
    GithubAppInstallationToken.http_client = nil
    @env.each do |key, value|
      value.nil? ? ENV.delete(key) : ENV[key] = value
    end
  end

  test "fetch mints and caches a GitHub App installation token" do
    now = Time.current
    install_env
    http = FakeGithubHttp.new([
      ok_response(token: "ghs_first", expires_at: 1.hour.from_now)
    ])
    GithubAppInstallationToken.http_client = http

    assert_equal "ghs_first", GithubAppInstallationToken.fetch(config, now: now)
    assert_equal "ghs_first", GithubAppInstallationToken.fetch(config, now: now + 1.minute)
    assert_equal 1, http.requests.length
    assert_match %r{/app/installations/456/access_tokens\z}, http.requests[0].path
  end

  test "fetch keeps a still-valid cached token when refresh fails" do
    now = Time.current
    install_env
    http = FakeGithubHttp.new([
      ok_response(token: "ghs_first", expires_at: now + 30.minutes),
      unauthorized_response
    ])
    GithubAppInstallationToken.http_client = http

    assert_equal "ghs_first", GithubAppInstallationToken.fetch(config, now: now)
    assert_equal "ghs_first", GithubAppInstallationToken.fetch(config, now: now + 20.minutes)
    assert_equal 2, http.requests.length
  end

  private

  def install_env
    key = OpenSSL::PKey::RSA.generate(2048)
    ENV["TEST_GITHUB_APP_ID"] = "123"
    ENV["TEST_GITHUB_INSTALLATION_ID"] = "456"
    ENV["TEST_GITHUB_PRIVATE_KEY_B64"] = Base64.strict_encode64(key.to_pem)
  end

  def config
    {
      "app_id_env" => "TEST_GITHUB_APP_ID",
      "installation_id_env" => "TEST_GITHUB_INSTALLATION_ID",
      "private_key_b64_env" => "TEST_GITHUB_PRIVATE_KEY_B64"
    }
  end

  def ok_response(token:, expires_at:)
    response = Net::HTTPOK.new("1.1", "200", "OK")
    response.instance_variable_set(
      :@body,
      JSON.generate({ "token" => token, "expires_at" => expires_at.iso8601 })
    )
    response
  end

  def unauthorized_response
    Net::HTTPUnauthorized.new("1.1", "401", "Unauthorized")
  end

  class FakeGithubHttp
    attr_reader :requests

    def initialize(responses)
      @responses = responses
      @requests = []
    end

    def request(uri, request)
      @requests << request
      @responses.shift
    end
  end
end
