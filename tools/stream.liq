# Modern-ish settings (set() is deprecated but still works; keeping it simple)
set("log.stdout", true)
set("log.level", 3)

# Resolve YouTube URL once at startup
yt_url =
  string.trim(
    process.read(
      "yt-dlp -f 'bestaudio*' -g https://www.youtube.com/watch?v=jfKfPfyJRdk"
    )
  )

radio = input.http(yt_url)

# Make it infallible so metadata.map can be applied
radio = mksafe(radio)

# Inject changing metadata
def inject_meta(_m) =
  ts = string(int(time()))
  [("artist", "Test Artist"), ("title", "Test Title " ^ ts)]
end

radio = metadata.map(inject_meta, radio)

# Push to Icecast as MP3 and send ICY metadata updates
output.icecast(
  %ffmpeg(
    format="mp3",
    %audio(codec="libmp3lame", b="128k", ar=44100, ac=2)
  ),
  host="127.0.0.1",
  port=8000,
  password="hackme",
  mount="/test.mp3",
  name="Liquidsoap ICY Test",
  genre="Test",
  description="ICY Metadata Test Stream",
  send_icy_metadata=true,
  icy_metadata=["song"],
  radio
)

