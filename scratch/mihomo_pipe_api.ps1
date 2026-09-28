param(
    [string]$Path = "/version",
    [string]$Method = "GET"
)
# 通过 mihomo 的命名管道控制器发 REST 请求（Verge Rev 2.x 无 TCP 控制端口）
$pipeName = 'verge-mihomo-production-18f75be94cf68bef763d82779a1ccc13312f3e99e4c96fa28a5ed572fdc95bec'
$pipe = New-Object System.IO.Pipes.NamedPipeClientStream('.', $pipeName, [System.IO.Pipes.PipeDirection]::InOut)
$pipe.Connect(3000)
$nl = [char]13 + [char]10
$req = "$Method $Path HTTP/1.1" + $nl + "Host: mihomo" + $nl + "Connection: close" + $nl + $nl
$bytes = [System.Text.Encoding]::UTF8.GetBytes($req)
$pipe.Write($bytes, 0, $bytes.Length)
$pipe.Flush()
$ms = New-Object System.IO.MemoryStream
$buf = New-Object byte[] 65536
try {
    while ($true) {
        $n = $pipe.Read($buf, 0, $buf.Length)
        if ($n -le 0) { break }
        $ms.Write($buf, 0, $n)
        if ($ms.Length -gt 20MB) { break }
    }
} catch {}
$pipe.Close()
[System.Text.Encoding]::UTF8.GetString($ms.ToArray())
