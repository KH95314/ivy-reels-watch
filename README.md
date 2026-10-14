# ivy-reels-watch

每天早上自动更新的 Instagram 爆款 Reels 创作者追踪库，为 Ivy 的三个账号提供选题灵感：
@ivyheartsgolf（高尔夫·自黑新手）、@ivyheartsfood（美食·$20以下/AYCE/探店）、@ivyluongg（美妆·GRWM/festival/wellness）。

种子名单来自 2026-10-13/14 的调研（35 位创作者实测，剔除爆款内容与赛道无关者后剩 29 个主名单＋4 个待补查），每周一还会从这批账号扩散发现新候选。

## 目录结构（Reels 和 profile 分开存）

```
seeds/watchlist.csv        追踪名单：handle,niche,status,added_date,source,notes
                           status: active（主名单）/ candidate（扩散发现的候选）/ appendix（待补查）/ removed

reels/latest.csv            全量 reels 库（按 shortcode 去重）：handle,niche,reel_url,shortcode,post_date,likes,first_seen,source
                            source: seed（调研时的代表作，只用于去重）/ pull（每日拉取，用于算平时中位数）
reels/daily/2026-10-14.csv  当天新发现的 reels（每天一个文件）

profiles/latest.csv         最新 profile 快照：handle,niche,followers,profile_url,checked_at
profiles/weekly/2026-10-14.csv  每周一的 profile 快照（粉丝数变化慢，每周刷一次）

briefing/2026-10-14.md      当天的简报：新爆款（点赞≥平时3倍）＋新候选账号＋运行备注
scripts/collect.py          采集脚本（cron 每天早上调用）
```

## 每天早上 7:30（洛杉矶时间）自动跑

1. `scripts/collect.py` 拉取名单账号的新 reels（instagram-cli，批量查，遇到 429 立停）
2. 新 reel 点赞 ≥ 该账号平时中位数 3 倍 → 记为爆款，写入当天 briefing
3. 每周一：刷新粉丝数＋扩散发现新候选（engagement 排序搜索，每赛道最多加 2 个）
4. 推送到 GitHub；有爆款/新候选才在聊天里发简报，没东西就静默更新

## 诚实声明

- Instagram API 不返回播放量，outlier 判定用「点赞 ≥ 平时中位数 3 倍」代理
- 只记录公开数据；全程只读，不关注/点赞/评论/私信
- 未核验的一律标 unknown，不编造
