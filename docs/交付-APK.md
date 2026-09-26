# 交付物 · Fund Insight 安卓安装包（APK）

给老板的一页说明：**这是什么、在哪个路径、怎么装、什么时候需要重新给包、有什么坑**。
每一条事实后面都跟着**当场复测的命令**（数字与结论不抄记忆，跑一遍看输出）。
复测时间：2026-09-26 14:49（北京）。

## 1. 包在哪

| 机型 | 路径 | 大小 |
| --- | --- | --- |
| 近几年的手机（arm64，绝大多数） | `E:\AI Agent\work area\fund-insight\dist\Fund-Insight-arm64-v1.1.apk` | 15.7 MB |
| 老机型（armv7） | `E:\AI Agent\work area\fund-insight\dist\Fund-Insight-armv7-v1.1.apk` | 13.0 MB |

⚠ `dist/` 在 `.gitignore` 里 ⇒ **APK 不进 GitHub**，只在这台机器上。复核：
`git check-ignore -v dist/Fund-Insight-arm64-v1.1.apk`。

## 2. 它是什么（这决定了"什么时候要重新给包"）

**壳 = 一个指向线上站点的 WebView**，不是把页面打包进 APK。
所以：

- **前端迭代不需要重新发版**。你装完之后，页面内容来自 `https://fund-insight.onrender.com`，
  我改完并部署，你下次打开就是新的（部署后旧 JS 缓存那件事已经由响应头
  `Cache-Control: no-cache, must-revalidate` 处理，见 `DEPLOYMENT.md`）。
- 需要重新给包只有两种情况：① 换了壳工程本身（`mobile/fund_insight_shell/`，例如改站点地址、
  改图标、改权限）；② 签名钥换了（见第 5 节）。

复测（只读，本机 → 线上）：

```bash
# 壳里那个地址（可被 --dart-define 覆盖，默认就是这个）
grep -n "kBaseUrl" mobile/fund_insight_shell/lib/main.dart
# 线上现在回答不回答（200 就说明壳打开后能拿到页面）
curl -s -o /dev/null -w '%{http_code}\n' https://fund-insight.onrender.com/api/health
```

## 3. 装上去要用什么登录

首次打开会要**访问口令**（就是生产的 `ACCESS_PASSWORD`，与网页版同一个）。
口令不在这个文档里、也不在 APK 里 —— 需要时从生产环境变量取（Render 控制台，或本机 `.env`）。
输过一次后存在手机本地，之后不用重复输。

## 4. 包内事实（当场用 aapt 量，不抄历史记录）

```bash
E:/Android/Sdk/build-tools/34.0.0/aapt.exe dump badging dist/Fund-Insight-arm64-v1.1.apk
```

```text
package: name='com.fundinsight.fund_insight_shell' versionCode='2002' versionName='1.1.0'
sdkVersion:'24'                    # Android 7.0 起
targetSdkVersion:'36'
uses-permission: android.permission.INTERNET      # 只有网络，没有存储/通讯录/定位
application-label:'Fund Insight'
```

⇒ 权限只有 `INTERNET` 一条（另有一条系统自动加的动态广播接收器权限，不涉用户数据）。

## 5. 签名的真相（这条会影响"你要不要先卸载"）

```bash
E:/Android/Sdk/build-tools/*/apksigner.bat verify --print-certs dist/Fund-Insight-arm64-v1.1.apk
# Signer #1 certificate DN: C=US, O=Android, CN=Android Debug
```

这个 "release" 包**用的是 Android 调试钥签的**（Flutter 模板默认：`android/app/build.gradle.kts`
的 release 块指向 `signingConfigs.getByName("debug")`）。含义分两种：

- **同一台机器重打包 → 可以直接覆盖安装**，你不用卸载，登录口令也还在。
- **换机器打包 / 换钥 / 调试钥被重建 → 签名不一致，手机会拒绝覆盖安装**，
  必须先卸载旧包再装新包（丢的只是 WebView 里存的访问口令，数据都在服务端）。

⇒ 所以：**我给新包时如果换了签名，会在这页明写"这次要先卸载"**。
若要彻底消除这个风险，需要给一个自有 upload 钥（钥文件与 `key.properties` 都不入库、
并约定备份位置）——这是你的决定项，我不会顺手换。

## 6. 边界（我没做过的事，别当成做过）

- **真机安装与使用属《需你实测清单》**：我这边没有连手机的调试通道，
  所以"装得上、登录进得去、手机上排版读得顺"这三件必须由你在手机上验一次，
  我不写成"已回归通过"。
- 我能给的证据止于：包内事实（第 4 节）、壳指向的站点在线且接口应答（第 2 节命令）、
  站点自身的新构建已由 `/api/health/detail` 自报 `git_commit` 证实（见 `DEPLOYMENT.md`）。
