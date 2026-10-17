# 离线 IP 地区数据

导出记录的地区解析使用 [ip2region 官方项目](https://github.com/lionsoul2014/ip2region/tree/v3.17.0) 的 IPv4 和 IPv6 XDB 数据。两份数据库随源码和后端镜像打包，默认即可使用；查询只读本地文件，没有 HTTP、DNS 查询或在线服务回退，不会把用户 IP 发给第三方。

## 来源与授权

- 固定上游版本：`v3.17.0`，Git 引用 `21ed71b0cce6b4f93529043ec618b3b7f396878e`。
- 下载日期：2026-10-17。原始文件未修改。
- 查询库：官方 Python 实现 `py-ip2region==3.0.4`。
- 上游作者：The Ip2Region Authors / Lionsoul。按照 Apache-2.0 使用，完整上游许可证保存在 [LICENSE.ip2region.md](LICENSE.ip2region.md)，其中亦包含作者提供的 MIT 选项。
- [官方 Python 用法与并发说明](https://github.com/lionsoul2014/ip2region/blob/v3.17.0/binding/python/README.md)。

| 文件 | 字节数 | SHA-256 |
| --- | ---: | --- |
| [ip2region_v4.xdb](https://raw.githubusercontent.com/lionsoul2014/ip2region/v3.17.0/data/ip2region_v4.xdb) | 11114380 | `6307a9696f5711f84bcb8b25f07894de68a64a0ed4a1cc7e990562dd3084f210` |
| [ip2region_v6.xdb](https://raw.githubusercontent.com/lionsoul2014/ip2region/v3.17.0/data/ip2region_v6.xdb) | 37258897 | `5b93da35ac28bc316dccc54a758381f7a874ae0461dd51ff5df5e34815586f11` |

数据库共约 46.1 MiB。运行时采用每份库 512 KiB 的向量索引，不把整份数据库载入内存；每个只读文件查询使用独立锁，并且最多缓存 4096 个公网地址的结果。初始化最多打开两个数据库文件。

## 显示规则

返回国家、省份、城市，省略数据库缺失值和重复地名，不显示运营商。中国地区的省市使用源数据中文，常见国家名称译成中文；海外州和城市保留源数据名称。IP 定位为数据库提供的近似网络地区，不代表用户的精确位置。

- 回环地址显示“本机”；RFC1918 私网和 IPv6 ULA 显示“内网”，例如 `172.20.0.1`。
- 链路本地、共享、组播、未指定及保留/测试地址分别标明地址类别，不猜测地理位置。
- 空值、格式无效、公网数据未命中或数据库不可用返回空值，由界面显示“—”。数据库缺失时内网/本机分类仍可用。
- 不直接用 Python 的 `is_private` 判断内网，因为该属性还包括文档和其他非公网地址。

## 更新

运行时不会自动下载数据。人工更新时从官方固定版本下载这两个 XDB，保留该版本的许可证并更新上表的来源、大小及 SHA-256，同时更新 `tests/test_ip_geolocation.py` 中固定快照及官方示例验证。先运行该测试，再构建后端和 worker 镜像并重新启动。新版数据须继续使用五段字段格式 `Country|Province|City|ISP|ISO-alpha2`。

替换本地数据库后重启服务即可重新加载。离线库有自己的更新周期，历史记录展示的是当前打包数据中的解析结果；不补写或推断旧记录缺失的 IP。
