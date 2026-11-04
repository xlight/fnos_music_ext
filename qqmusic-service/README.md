# qqmusic-service

HTTP 包装 [L-1124/QQMusicApi](https://github.com/L-1124/QQMusicApi)（PyPI：`qqmusic-api-python`），供 fnmusic-ext 作为 QQ 音乐音源（默认 `127.0.0.1:8775`）。

接口形状与 `musicbox-service`（网易云）保持一致：以歌曲 **mid** 作为 `song_id`，沿用 `song_name` / `artist` / `album_name` / `duration` / `album_pic_url` 等字段名，proxy 的搜索与详情链路可直接复用。

## 音质与登录

QQ 音乐按权益分级下发链接：**未登录只能取到 128k MP3**，320k / 无损需要扫码登录后按账号权益获取。取链会按音质偏好由高到低回退（高音质无权限时降级到可播档），不会绕过账号权限。

扫码登录：`POST /api/v1/auth/login` 取 `qr_id` → `GET /api/v1/auth/login/qr.png?id=` 展示二维码 → 轮询 `GET /api/v1/auth/login/check?id=`。状态码语义与网易对齐（801 等待 / 802 已扫码 / 803 成功 / 800 过期 / 804 拒绝）。

凭证持久化在数据目录的 `credential.json`（`QQMUSIC_DATA_DIR` 或 `$XDG_DATA_HOME/qqmusic`），设备信息同目录 `device.json` 保持固定。

## 本地运行

```bash
pip install -r requirements.txt
uvicorn app:app --host 127.0.0.1 --port 8775
curl -s 'http://127.0.0.1:8775/api/v1/search?keyword=%E5%91%A8%E6%9D%B0%E4%BC%A6&limit=5'
```
