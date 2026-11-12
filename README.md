# HFT Self-Improvement Loop (Local)

Grok Bot を司令塔に、ローカルで **データレイク → レイテンシ補正 → バックテスト → 知見蓄積** を回す自己改善 HFT 基盤です。  
実行の正は常に `scripts/*.py`。Bot は `bot_prompts/` の指示に従い、stdout の JSON と `experiments/` / `knowledge/` だけを根拠に判断します。

## ディレクトリ構成

```
data/
  raw/                 # プロバイダ生データ
  parquet/             # 加工済み Parquet（ヒストリカル）
  live/                # リアルタイム相当（local_ts 付き）
experiments/           # 実験成果（metrics / trades / equity 等）
knowledge/
  findings.md          # 確定知見のみ
  experiments_index.md # 実験一覧
scripts/               # CLI（Bot 許可リスト対象）
strategies/            # 差し替え可能な戦略実装
bot_prompts/           # Grok に渡す役割別プロンプト
config/
  settings.yaml        # パス・BT・許可スクリプト
  strategies/          # 戦略パラメータ YAML
src/hft_loop/          # 共有ライブラリ
```

## セットアップ

```bash
python3.11 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
# 必要に応じて編集:
#   TARDIS_API_KEY=...
#   DATABENTO_API_KEY=...
#   GROK_API_KEY=...
```

| 設定 | 場所 |
|------|------|
| パス・BT・Grok 許可リスト | `config/settings.yaml` |
| 戦略パラメータ | `config/strategies/*.yaml` |
| 秘密情報 | `.env` のみ（YAML に書かない） |

疎通:

```bash
python scripts/ops_check.py
```

## スクリプト一覧

共通仕様:

- プロジェクトルートから実行
- **stdout = JSON 1オブジェクト**（`ok: true/false`）
- **stderr = 構造化ログ**（structlog）
- 任意で `--json-out path` / `--config path/to/settings.yaml`

### `scripts/ops_check.py`

環境・パス・依存・許可スクリプトの有無を確認。

```bash
python scripts/ops_check.py
```

### `scripts/download_data.py`

Tardis / Databento 取得（キー必須）。キー無しは `--dry-run` で合成 Parquet。

```bash
# オフライン検証
python scripts/download_data.py --dry-run --days 1
python scripts/download_data.py --dry-run --as-live --days 1   # → data/live

# 実データ
python scripts/download_data.py --provider tardis \
  --symbol BTC-PERPETUAL --venue deribit --dataset trades \
  --start 2024-01-01 --end 2024-01-02
```

### `scripts/inspect_data.py`

最新（または指定）Parquet の行数・欠損・基本統計。

```bash
python scripts/inspect_data.py
python scripts/inspect_data.py --root live
python scripts/inspect_data.py --path data/parquet/tardis/BTC-PERPETUAL
```

### `scripts/latency_correction.py`

live と historical を比較し mean / jitter を推定。補正済みデータを `experiments/<run_id>/` に保存。

```bash
python scripts/latency_correction.py --auto-latest
python scripts/latency_correction.py --apply-only --auto-latest --latency-ms 5
```

### `scripts/backtest.py`

戦略バックテスト。結果サマリーを stdout、詳細を `experiments/<run_id>/` へ。

```bash
python scripts/backtest.py --list-strategies
python scripts/backtest.py --strategy dummy_mean_reversion \
  --data data/parquet --latency-ms 5 --run-id exp_demo
python scripts/backtest.py \
  --strategy-config config/strategies/dummy_mean_reversion.yaml \
  --data data/parquet --param window=40 --device cpu
```

成果物例: `metrics.json`, `result.json`, `trades.json`, `equity_curve.parquet`, `summary.md`

## データレイク更新フロー

```text
[Provider / dry-run]
        │
        ▼
   data/raw/          ← 生データ（任意）
        │
        ▼
 data/parquet/        ← ヒストリカル（BT入力）
        │
 data/live/           ← local_ts 付き（遅延推定）
        │
        ├─ inspect_data.py          品質確認
        ├─ latency_correction.py    mean/jitter → experiments/
        └─ backtest.py              戦略評価 → experiments/
                                        │
                                        ▼
                              knowledge/findings.md
                         （Curator が確定分のみ昇格）
```

推奨の手動ワンパス:

```bash
python scripts/download_data.py --dry-run --days 1
python scripts/download_data.py --dry-run --as-live --days 1
python scripts/inspect_data.py --root parquet
python scripts/latency_correction.py --auto-latest
python scripts/backtest.py --strategy dummy_mean_reversion --data data/parquet --latency-ms 5
```

## Grok Bot 連携

### 役割とプロンプト

| 役割 | プロンプト | いつ使うか |
|------|------------|------------|
| Improver | `bot_prompts/improver_routine.md` | 30–60分ごとの自己改善ルーチン |
| Curator | `bot_prompts/knowledge_curator.md` | 複数実験から findings を昇格 |
| Ops Guard | `bot_prompts/ops_guard.md` | 遅延・欠損・メンテ監視、停止推奨 |
| Commander | `bot_prompts/commander.md` | 全体方針の短い参照用 |

許可スクリプトは `config/settings.yaml` → `grok.allowed_scripts`。

### Routine への渡し方

1. Bot / Agent のシステム（または Routine）指示に、使う役割の Markdown **全文**を貼る  
   - 例: 定期 Improver なら `improver_routine.md` のみ  
   - 併用するなら Guard → Improver の順（Guard が `STOP_RECOMMENDED` なら実験しない）
2. ワーキングディレクトリはリポジトリルート。Python は venv を有効化済みであること。
3. Bot には「**stdout JSON のみをパース**し、stderr はログとして無視してよい」とプロンプト内で指示済み。
4. 実験の正本は `experiments/<run_id>/metrics.json`（＋ `result.json`）。知見の正本は `knowledge/findings.md`。
5. Improver が findings 追記案を出したら、確定前に Curator プロンプトで再審査するのが安全。

最小の Improver 起動イメージ:

```text
System: (improver_routine.md の内容)
User: 定期ルーチンを1回実行。プロジェクトルートは /path/to/this/repo。
```

Bot が叩くコマンド例は各プロンプト内の「Commands」節を優先する。

## 注意事項

**APIキー**

- `.env` または環境変数のみ。`settings.yaml` / プロンプト / commits に書かない
- `.env` は gitignore 済み。共有はチームの秘密管理経由で

**リスク管理**

- 現状の戦略は **ダミー**。dry-run 合成データでの結果を本番性能とみなさない
- Ops Guard が停止推奨を出している間は、新規バックテスト量産や live 前提判断を止める
- レイテンシ未補正のまま HFT 仮説を断定しない
- Bot は発注・資金移動を行わない（診断とローカル実験まで）

**その他**

- Python 3.11+
- Databento 実取得には別途 `pip install databento` が必要
- GPU は `--device cuda` を戦略へ渡すフックのみ（ダミー戦略は CPU）
