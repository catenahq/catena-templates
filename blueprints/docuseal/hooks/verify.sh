ruby -rnet/http -rjson -e '
  get = lambda do |path|
    Net::HTTP.start("127.0.0.1", 3000, open_timeout: 10, read_timeout: 20) do |http|
      http.request(Net::HTTP::Get.new(path, "X-Forwarded-Proto" => "https"))
    end
  end
  begin
    up = get.call("/up")
    manifest = get.call("/manifest")
  rescue SystemCallError, Timeout::Error, SocketError => e
    abort "DocuSeal does not answer on 3000: #{e.message}"
  end
  abort "DocuSeal /up answered #{up.code}" unless up.code == "200"
  name = begin
    JSON.parse(manifest.body)["name"]
  rescue JSON::ParserError
    nil
  end
  abort "DocuSeal /manifest answered #{manifest.code} without the product name" unless manifest.code == "200" && name == "DocuSeal"
  puts "DocuSeal answers on 3000"
'
