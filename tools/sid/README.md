# 抓 SID 用的组件

抓 SID 需要在这里放一个抓包用的程序：

```
tools/sid/SID抓取后端.exe
```

这个 exe 大约 31MB，仓库里没放。抓包时程序会这样把它带起来：

```
SID抓取后端.exe --mitm -q -p <端口号> -s <本目录>/sid_addon.py
```

`sid_addon.py` 负责从请求里找这些东西：

- `sid`
- `auth_type`（3 是店主，4 是店员）
- `mch_id`、`shop_id`、`v`

请求的网址参数、JSON 内容、请求头（`sid` / `X-Sid`）和 cookie 里都会找。

`mitmproxy-ca-cert.pem` 是抓包用的证书，第一次要装上。
