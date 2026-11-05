# SID 抓取组件

抓 SID 需要在这里放一个抓包后端：

```
tools/sid/SID抓取后端.exe
```

仓库不提交这个 exe（约 31MB）。程序启动抓包时会执行：

```
SID抓取后端.exe --mitm -q -p <端口> -s <本目录>/sid_addon.py
```

`sid_addon.py` 从请求中提取：

- `sid`
- `auth_type`（3 店主 / 4 店员）
- `mch_id`、`shop_id`、`v`

来源包括 query string、JSON body、header（`sid` / `X-Sid`）和 cookie。

`mitmproxy-ca-cert.pem` 用于首次安装抓包证书。
