# krflow — 국내주식 수급 전략 검증·모의매매

기관 3거래일 연속 순매수 + 마지막 신호일 외국인 순매수 종목을 다음 거래일 시가에 사서, 매수일 포함 5번째 거래일 종가에 파는
전략을 **검증**하고 **모의투자**로 운용하기 위한 프로그램이다. 실계좌 주문은 코드 수준에서 차단되어 있다.

우선순위는 수익률이 아니라 **결과의 재현성, 데이터의 정확성, 주문의 안정성**이다.

## 세 가지 결과의 구분

| 구분 | 표시 | 의미 |
|---|---|---|
| 합성 데이터 테스트 | `synthetic` (노란 배너) | 시드로 만든 가짜 시세·수급. 엔진이 올바르게 동작하는지 확인하는 용도. **수익률은 아무 의미 없음** |
| 실제 데이터 백테스트 | `real_backtest` | CSV/pykrx로 들여온 과거 데이터. 과거 결과일 뿐 미래 수익을 보장하지 않음 |
| 증권사 모의투자 | `broker_paper` / `kis_vts` (초록 배너) | 한국투자증권 모의투자 계좌 체결 결과. **연동 코드는 미검증** (아래 참조) |

모든 저장 결과(`results/*/summary.json`)에는 `data_kind`, 설정 전문, 데이터 지문(해시), 경고가 함께 들어간다.

## 연동 검증 상태 (정직하게)

| 기능 | 상태 | 비고 |
|---|---|---|
| 합성 데이터 생성·품질검사·백테스트·비교 | **검증** (단위테스트 22개) | |
| 내장 페이퍼 브로커 모의매매·재시작 복구·잔고 대조·중단/재개 | **검증** (단위테스트) | 통신 장애 시 UNKNOWN 처리도 테스트됨 |
| 로컬 대시보드 | **검증** (수동 확인) | `http://127.0.0.1:8765` |
| CSV 실제 데이터 불러오기 | **검증** (단위테스트) | 컬럼 형식은 `scripts/sample_*.csv` |
| pykrx 실제 데이터 수집 | **미검증** | 개발 환경에서 KRX 접속 불가. 상장폐지 종목 누락(생존편향) 경고 포함 |
| 한국투자증권 모의투자 주문/조회/잔고 | **미검증** | 개발 환경에서 증권사 접속 불가. 공개 문서 기준으로 작성. `kis-check`로 먼저 확인할 것 |
| 실계좌 주문 | **차단** | `mode=real` 설정 시 즉시 예외, 실전 TR ID 사용 불가 |

## 빠른 시작 (Windows)

```
pip install -r requirements.txt
scripts\synthetic_demo.bat     REM 합성 데이터 → 전략 비교 → 페이퍼 모의매매
scripts\dashboard.bat          REM 대시보드 http://127.0.0.1:8765
scripts\run_tests.bat
```

명령 직접 실행 (저장소 루트에서, `set PYTHONPATH=%CD%`):

```
python -m krflow.cli init-config                                             # config.json 생성
python -m krflow.cli --db data\s.db synth --start 2024-01-01 --end 2025-12-31 --codes 40 --seed 7 --inject-issues
python -m krflow.cli --db data\s.db quality
python -m krflow.cli --db data\s.db backtest --start 2024-02-01 --end 2025-12-31
python -m krflow.cli --db data\s.db compare  --start 2024-02-01 --end 2025-12-31 --split 2025-01-01
python -m krflow.cli --db data\s.db paper    --start 2025-06-01 --end 2025-12-31 -v
python -m krflow.cli --db data\s.db dashboard
python -m krflow.cli --db data\s.db halt --reason "점검"   /  resume
```

실제 데이터:

```
python -m krflow.cli --db data\real.db import-csv --bars bars.csv --flows flows.csv
python -m krflow.cli --db data\real.db collect-pykrx --start 2024-01-01 --end 2025-12-31 --codes 005930,000660   [미검증]
python -m krflow.cli --db data\real.db compare --start 2024-02-01 --end 2025-12-31 --split 2025-01-01
```

한국투자증권 모의투자 [미검증]: 환경변수 `KIS_APP_KEY`, `KIS_APP_SECRET`, `KIS_ACCOUNT_NO`(예 `50123456-01`) 설정 후
`scripts\kis_vts_serve.bat`. 인증정보는 환경변수에서만 읽고 화면·로그·결과 파일에 쓰지 않는다(`kis-check`는 마스킹 출력).

## 구조

```
krflow/
  config.py            설정, 실행 모드, 실계좌 차단, 비용 모델
  calendar.py          거래일/휴장일/특별거래시간
  db.py                SQLite 스키마 (시세·수급·주문 원장·포지션·이벤트·대조 기록)
  data/  store.py      point-in-time 조회(available_at), 데이터 지문, 수집 로그
         quality.py    누락·중복·비정상·단위불일치·잠정치·지연·정정·기업행동 미반영 검사 → 매매 차단
         synthetic.py  시드 기반 합성 데이터 (flow_effect로 엔진 검증)
         csv_source.py / pykrx_source.py[미검증]
  strategy/signal.py   후보 산출·순위·선정/제외 근거
  backtest/ engine.py  거래일 루프 (시가 매수 → 종가 청산 → 평가), 청산 불가 이월
            metrics.py compare.py(전략 변형·비용 민감도·학습/검증 분리·벤치마크) report.py
  broker/   base.py    주문 상태: new/submitted/unfilled/partial/filled/rejected/cancel_requested/cancelled/unknown
            ledger.py  전송 전 기록(write-ahead), 결정적 client_order_id, UNKNOWN 처리, 조회로 해결
            paper.py   내장 페이퍼 브로커 (장애 주입 가능)
            kis.py     한국투자증권 VTS 어댑터 [미검증]
  risk/limits.py       건별·일별·보유수 한도(미체결 포함), 손실 중단, 수동 중단, 차단 사유
  ops/runner.py        open/close/eod 단계, 재시작 복구, 잔고 대조, 단계 재실행 방지
  ui/server.py         로컬 대시보드 (표준 라이브러리 http.server)
tests/                 pytest
```

## 매매 규칙의 정확한 정의

- 신호일 T: 기관 순매수(거래대금 기준) > 0 이 T-2, T-1, T 모두 성립. 기본 전략은 T의 외국인 순매수 > 0 추가.
- T의 수급 데이터는 T+1 `decision_time`(기본 08:30) 이전에 `available_at`이 찍혀 있어야 사용한다. 잠정치, 단위 불일치, 지연 데이터는 제외되고 사유가 표시된다.
- T+1 시가 매수(시장가, 슬리피지 불리 방향 가정). 매수일 포함 5번째 거래일 종가 매도. 거래정지 등으로 불가하면 다음 거래일 재시도하고 상태(`exit_blocked`)와 이월 사유를 기록한다.
- 순위: 기본 `inst_sum_ratio` = 3일 기관 순매수 합 / 3일 거래대금 합. 대안 `inst_sum`, `foreign_net`.
- 동일 종목은 보유 중이거나 미해결 주문이 있으면 다시 사지 않는다. 미체결 매수도 보유 한도와 주문가능금액에서 차감한다.

## 검증 한계 (결과를 볼 때 반드시 같이 볼 것)

- **데이터 공개 시점**: KRX 투자자별 매매동향 확정치는 장 마감 후 공개된다. CSV/pykrx 데이터에 시각이 없으면 "기준일 18:00"으로 가정하며 이 가정은 수집 로그에 기록된다. 장중 잠정치로 신호를 만들면 확정치와 달라질 수 있다.
- **생존편향**: pykrx 등 현재 상장 종목 목록으로 과거를 돌리면 상장폐지 종목이 빠진다. `universe` 테이블에 상폐일을 넣지 않으면 결과에 편향이 남는다.
- **수정주가**: 원주가 기준으로 저장하고 `adj_factor`를 별도 관리한다. 분할·병합 당일 ±35% 초과 변동은 기업행동 등록이 없으면 `abnormal_jump`로 차단된다.
- **체결 가정**: 시가/종가 시장가 체결, 호가·유동성·부분체결은 백테스트에 없다. 모의투자 단계에서 실제 체결과 비교해야 한다.
- **손실 중단**: 백테스트에도 손실 한도(기본 초기자금의 10%)가 적용되어 경로 의존성이 생긴다. 비용 민감도는 `avg_trade_return`(거래당 평균)으로 보는 것이 선형적이다.
- **다중 비교**: 변형 4개 × 비용 6개를 돌려 가장 좋은 것을 고르면 과최적화다. 학습기간에서 고른 설정을 검증기간에 그대로 적용해야 한다.

## 이 전략이 수익을 낼 수 있는가

정직한 판단은 "**기본 형태로는 어렵고, 비용을 넘길 만한 초과수익이 있는지 실제 데이터로 확인하기 전까지는 판단 유보**"다.

- 왕복 비용이 크다. 수수료 0.015%×2 + 매도세 0.15% + 슬리피지 0.1%×2 ≈ **0.38%/거래**. 5거래일 보유 전략이 이걸 넘으려면 거래당 평균 0.4% 이상의 초과수익이 꾸준히 필요하다.
- 기관·외국인 순매수와 수익률의 관계는 대체로 **동시적**(같은 날 주가가 오르면서 순매수가 잡힘)이고, 신호 다음날부터의 추가 수익은 학술 연구에서도 작고 불안정하게 보고된다. 3일 연속 순매수 종목은 이미 올라 있는 경우가 많아 단기 되돌림 위험도 있다.
- 신호가 흔하다(합성 데이터에서 하루 3~5개). 예산·보유 한도 때문에 순위 규칙이 결과를 크게 좌우하며, 합성 데이터에서도 순위 방식만 바꿔서 ±15%p가 움직였다. 이는 순위 규칙 선택이 과최적화 지점이 되기 쉽다는 뜻이다.
- 따라서 순서는 ① CSV/pykrx로 2~3년 실제 데이터 확보 → ② `compare --split`으로 학습/검증 분리 → ③ 두 구간 모두 `avg_trade_return`이 비용(0.38%) 이상이고 벤치마크를 이기는지 확인 → ④ 그때만 모의투자 3개월 이상 → ⑤ 모의투자 체결과 백테스트 가정의 차이(슬리피지, 미체결) 측정. 이 중 하나라도 실패하면 실계좌는 고려하지 않는다.

이 저장소의 합성 데이터 결과(예: `scripts\synthetic_demo.bat` 출력)는 위 판단과 무관하다. 합성 데이터에 `--flow-effect 0.3`을 주면 승률 77%가 나오는데, 이는 엔진이 수급 효과를 탐지할 수 있음을 보여줄 뿐이다.
