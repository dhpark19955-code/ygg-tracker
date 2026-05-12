# YGG Competitor Tracker

Yanolja Go Global의 B2B 베드뱅크/유통 피어 3사 (**Web Travel Group**, **TBO Tek**, **HBX Group**) 주가·밸류에이션·뉴스 트래커.

GitHub Actions가 매일 자동으로 데이터를 가져와 `data.json`을 갱신하고, GitHub Pages로 정적 호스팅됩니다. API 키 불필요.

## 구조

```
ygg-tracker/
├── index.html                    # 대시보드 (data.json을 fetch)
├── data.json                     # 매일 GitHub Actions가 덮어씀
├── requirements.txt              # Python deps (yfinance, feedparser, requests)
├── scripts/
│   └── fetch_data.py             # 데이터 수집 스크립트
└── .github/
    └── workflows/
        └── update.yml            # 매일 23:00 UTC (08:00 KST) 실행
```

## 셋업 (한 번만)

### 1. GitHub에 repo 만들기

```bash
gh repo create ygg-tracker --public --source=. --remote=origin --push
# 또는 웹 UI에서 빈 repo 생성 후:
git init
git add .
git commit -m "Initial setup"
git branch -M main
git remote add origin https://github.com/<your-username>/ygg-tracker.git
git push -u origin main
```

### 2. GitHub Pages 활성화

Repo → **Settings → Pages** → Source: `Deploy from a branch` → Branch: `main` / root → Save.

1-2분 뒤 `https://<your-username>.github.io/ygg-tracker/` 에서 접속 가능.

### 3. Actions 권한 확인

Repo → **Settings → Actions → General → Workflow permissions** → **Read and write permissions** 체크.

이게 활성화돼야 워크플로우가 `data.json`을 commit할 수 있음.

### 4. 첫 데이터 수집 트리거

Repo → **Actions** 탭 → `Update tracker data` → **Run workflow** 클릭.

또는 그냥 다음 날 08:00 KST까지 기다리면 자동 실행.

## 로컬 테스트

```bash
pip install -r requirements.txt
python scripts/fetch_data.py
# data.json 갱신됨

# 페이지를 로컬에서 띄우기 (file:// 직접 열면 CORS로 fetch가 막힘)
python -m http.server 8000
# 브라우저로 http://localhost:8000 접속
```

## 커스터마이징

### 종목 추가/변경

`scripts/fetch_data.py`의 `TICKERS` dict에 항목 추가:

```python
TICKERS = {
    "AMZN": {
        "yf": "AMZN",
        "ccy": "USD",
        "ccy_symbol": "$",
        "news_q": '"Amazon"',
        "name": "Amazon",
        "exchange": "NASDAQ",
    },
    ...
}
```

그리고 `index.html`의 `TICKERS` 배열과 `STATIC` 객체도 같이 업데이트.

### 갱신 주기 변경

`.github/workflows/update.yml`의 `cron` 표현식 수정:

```yaml
- cron: "0 23 * * *"   # 매일 23:00 UTC = 08:00 KST
- cron: "0 */6 * * *"  # 6시간마다 (장 마감 따라가기)
```

GitHub Actions cron은 UTC 기준이고, 무료 plan에서도 일 단위 실행 가능. 다만 정시에 정확히 돌지는 않음 (최대 15분 정도 지연).

### 뉴스 검색어 튜닝

`TICKERS[key].news_q`에 Google News 검색 문법 그대로 사용 가능:

```python
"news_q": '"HBX Group" OR Hotelbeds -hotel'  # 호텔 일반 뉴스 제외
```

## 트러블슈팅

**`data.json`이 갱신되지 않음**
- Actions 탭에서 워크플로우 실행 로그 확인
- Settings → Actions → Workflow permissions가 `Read and write`인지 확인
- yfinance가 일시적으로 막힌 경우 (rate limit) 다음 실행에서 복구됨

**일부 종목의 EBITDA / P/E가 비어있음**
- yfinance는 인도(NSE)·스페인(BME) 종목의 fundamentals를 항상 제공하지 않음
- 가격·시가총액·52W는 거의 항상 가져옴

**뉴스가 빈 채로 나옴**
- Google News RSS가 일시적으로 막혔거나 검색어에 매치되는 결과가 없음
- 검색어를 단순화해서 (`"TBO Tek"` → `TBO Tek`) 다시 시도

## 데이터 소스

- **주가/밸류에이션**: [yfinance](https://github.com/ranaroussi/yfinance) (Yahoo Finance unofficial API)
- **뉴스**: Google News RSS
- **FX**: yfinance의 `XXXUSD=X` ticker, 실패 시 하드코딩 fallback

모두 무료 + API 키 불필요.

## 라이선스

내부용. 데이터는 거래 목적이 아닌 참고용입니다.
