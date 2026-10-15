# iOS 信封观测（脱敏版）

记录 iOS 客户端收发包的信封形态（**不含任何真机设备码或记录号**）。

- **User-Agent**：`StudentsMarch/<appVersion> (iPhone; iOS <系统版本>; Scale/3.00)`
- **`model` 头**：纯机型市场名（如 `iPhone 14 Pro Max`），且**只出现在 `auth/oauth/token` 那一条**
- **设备码**：`uniqueCode` 为 **UUID 形态**，由 App 自行生成（非硬件派生）；本仓库不携带任何真机值，
  改为**每台机首启随机生成并持久化**（见 `device_profile._local_ios_code`）
- **签名**：业务请求带 `timestamp`(ms) + `sign`（32 位小写 hex）；
  算法为 `md5(AES(sorted(k=v&) + appId' + appSecret'))`，key = `md5(token)[:16]`、iv = `md5(token)[16:32]`
- **加密参数**：业务字段打包进 AES 密文 `param`，AES 密钥每请求随机并由 RSA 包进 `key`

以上均只描述**协议形态**，与具体账号 / 设备无关。
