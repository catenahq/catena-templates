ruby -rnet/http -rjson -e '
  stop = Process.clock_gettime(Process::CLOCK_MONOTONIC) + 150
  begin
    res = Net::HTTP.start("127.0.0.1", 3000, open_timeout: 10, read_timeout: 20) do |http|
      http.request(Net::HTTP::Get.new("/api", "X-Forwarded-Proto" => "https"))
    end
  rescue Errno::ECONNREFUSED => e
    abort "Chatwoot does not answer on 3000: #{e.message}" if Process.clock_gettime(Process::CLOCK_MONOTONIC) >= stop
    sleep 4
    retry
  rescue SystemCallError, Timeout::Error, SocketError => e
    abort "Chatwoot does not answer on 3000: #{e.message}"
  end
  body = begin
    JSON.parse(res.body)
  rescue JSON::ParserError
    {}
  end
  abort "Chatwoot /api answered #{res.code} without a version" unless res.code == "200" && !body["version"].to_s.empty?
  abort "Chatwoot reports Redis #{body["queue_services"]} and Postgres #{body["data_services"]}" unless body["queue_services"] == "ok" && body["data_services"] == "ok"
  puts "Chatwoot #{body["version"]} answers, Redis and Postgres ok"
'
