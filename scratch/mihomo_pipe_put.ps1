param(
    [string]$Path = "",
    [string]$Body = ""
)
# ASCII-only: talk HTTP over mihomo named-pipe controller
$pipeName = "verge-mihomo-production-18f75be94cf68bef763d82779a1ccc13312f3e99e4c96fa28a5ed572fdc95bec"
$p = New-Object System.IO.Pipes.NamedPipeClientStream(".", $pipeName, [System.IO.Pipes.PipeDirection]::InOut)
$p.Connect(3000)
$crlf = [char]13 + [char]10
$bodyBytes = [System.Text.Encoding]::UTF8.GetBytes($Body)
$req = "PUT " + $Path + " HTTP/1.1" + $crlf +
       "Host: mihomo" + $crlf +
       "Content-Type: application/json" + $crlf +
       "Content-Length: " + $bodyBytes.Length + $crlf +
       "Connection: close" + $crlf + $crlf
$head = [System.Text.Encoding]::UTF8.GetBytes($req)
$p.Write($head, 0, $head.Length)
if ($bodyBytes.Length -gt 0) { $p.Write($bodyBytes, 0, $bodyBytes.Length) }
$p.Flush()
$ms = New-Object System.IO.MemoryStream
$buf = New-Object byte[] 65536
while (($k = $p.Read($buf, 0, $buf.Length)) -gt 0) { $ms.Write($buf, 0, $k) }
$p.Close()
[System.Text.Encoding]::UTF8.GetString($ms.ToArray())
