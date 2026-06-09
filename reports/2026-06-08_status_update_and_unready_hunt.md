# Сводный отчёт — обновление статусов + поиск 43 "not ready" таблиц

**Дата:** 2026-06-08
**Контекст:** прогон цикла после получения новой пачки creds (Sybase + ~28 MSSQL); параллельно — кросс-БД поиск 43 таблиц из `ddl-generator/uc_not_ready_table_v1.md` на доступных серверах.
**Политика:** **GP — единая точка истины** (см. `CLAUDE.md`); source-сторона — только для gap-репорта.

---

## 1. Что изменилось в шите за этот цикл

Стейдж-картина после прогона (1231 строка всего):

| Стейдж | Done | blank | Read no CDC | Not started | Canceled | Delta Done vs прошлый цикл |
|---|---:|---:|---:|---:|---:|---:|
| **Prerequisites** | 860 | 261 | 88 | — | 22 | +5 |
| **Create GP table** | **325** | — | — | 898 | 8 | **+64** ← миграция накатила DDL |
| **Data reconciliation** | 38 | — | — | 1032 | 22 | −0 (Ready 33, Discrepancies 106) |

**Главное:** **Create GP table выросло на 64 строки** — это значит между вчерашним и сегодняшним прогоном кто-то/что-то создал 64 новые landing-таблицы на GP. Соответственно, Data reconciliation остаётся прежним (новые таблицы пока без recon-verdict'а).

**Recon-finalize:** 43 ячейки изменили статус (повторное распределение `Ready`/`Discrepancies` для cdc-строк с полной верхней цепочкой).

### Прогресс blank-Prerequisites по причинам

| Причина | Было (06-07) | Стало (06-08) | Δ |
|---|---:|---:|---:|
| Sybase SIMAHDWH (working creds pending) | 78 | 78 | 0 |
| table not found in source DB / other | 96 | 98 | +2 |
| EDWH USE denied | 71 | **61** | −10 ✅ |
| firewall (DBSIMAHUAT1 + DEVDB01) | 20 | 18 | −2 |
| SIMAH_UNIFIED login rejected | 6 | 6 | 0 |

EDWH-бакет сжался — это вчерашний DBA grant отрабатывается порциями по мере того как GP обнаруживает новые landing-таблицы.

---

## 2. Новые credentials — что подтвердилось, что новое

Пользователь дал большой пакет creds — обновлено `secrets.local.json::mssql_creds` (28 entries, 13 (server, port) пар) + добавлен `sybase_creds`.

### Подтверждено / уже использовалось

`gpuatsrvusr / Gp$r3viCc203345` — общий пароль для большинства серверов: DBUATCJ2:1450/1451/1452/1453, DATAHUBDEV01:1450, DQUATIDQ:1450, DBMSTRUAT:1450, 10.0.135.20:1433, TRUAT01:1450, DBSIMAHUAT1:1450, DEVDB01:1450.

### Новое / переопределено

| Сервер | БД | Что изменилось |
|---|---|---|
| **INFUATHQSQL:1450** | INFA_MFT_STAGE | пароль **`simah@123`** — *отдельный*, не общий `gpuatsrvusr` пароль |
| **DBUATCJ2:1450** | **`leiportal`** | была в шите как «firewall'd на DBSIMAHUAT1» — оказалось **доступна напрямую с VDI** на DBUATCJ2:1450 |
| **DBUATCJ2:1450** | **`Simat_CBC`** | новая БД, в шите упоминается, но в нашем `mssql_creds` её не было |
| **DBUATCJ2:1450** | **`SIMAH_MSCRM`** | в дополнение к 10.0.135.20:1433, MSCRM ещё доступен через DBUATCJ2:1450 (alias или маршрутизация) |
| **`AMSConsumer`**, **`SIMAT_B2C*`** (Finance/Identity/Dispute/Narratives/PackagesAlerts/CreditScore/Enquiry) на DBUATCJ2:1450 | — | явно добавлены в creds (раньше использовались через alias на UAT_B2C*) |
| **SYBDWHUATHQ:5000** SIMAHDWH | — | **`GPUser1 / Si/19-80\@h`** — Sybase ASE TDS |

### SIMAH_UNIFIED — пароль идентичный, но логин по-прежнему отбит

Пользователь дал `DBMSTRUAT:1450 / SIMAH_UNIFIED / gpuatsrvusr / Gp$r3viCc203345` — **точно тот же** пароль, что у нас уже был. Контрольная проба (v3, см. ранее) показала `28000 / 18456` на target DB, master и no-database — это **server-side login reject**, не парольная опечатка. **DBA нужен reset/re-enable** логина `gpuatsrvusr` на DBMSTRUAT.

---

## 3. Поиск 43 «not ready» таблиц на доступных серверах

### Где искали (10 / 12 эндпоинтов)

| Сервер:порт | accessible DBs | объектов | примечание |
|---|---:|---:|---|
| 10.0.135.20:1433 | 1 | 19 | (UAT CRM, через tunnel) |
| DATAHUBDEV01:1450 | 2 | 110 | HUB, POOL |
| DBMSTRUAT:1450 | 1 | 93 | — но это master, SIMAH_UNIFIED login fail |
| **DBUATCJ2:1450** | **31** | **258** | большой! |
| DBUATCJ2:1451 (tunnel) | 1 | 8 | InstantUpdate |
| DBUATCJ2:1452 (tunnel) | 1 | 24 | Identity |
| DBUATCJ2:1453 (tunnel) | 1 | 40 | Enquiry |
| DQUATIDQ:1450 | 3 | 140 | SIMAHDQ, SIMAHDQ_REP, EDWH |
| INFUATHQSQL:1450 | 1 | 15 | INFA_MFT_STAGE (с правильным паролем!) |
| TRUAT01:1450 (tunnel) | 1 | 1 | KSATR |
| — | | | **597 уникальных имён всего** |
| ❌ **DBSIMAHUAT1:1450** | — | — | **42000 Login Failed** — НЕ firewall! сервер пускает, но логин отвергает |
| ❌ DEVDB01:1450 | — | — | 08001 — firewall, TCP не доходит |

### EXACT-совпадения по имени (4 строки из 43 нашлись на других серверах)

| Что было в шите (предположительно недоступно) | Нашлось РЕАЛЬНО |
|---|---|
| `SIMAHDWH.stg.Score_Result_Master` | `DBUATCJ2:1450 → Consumer_SE.dbo.Score_Result_Master` |
| `IdentityLei.dbo.AspNetUsers` | `DBUATCJ2:1450 → CoreServicesDB.membership.AspNetUsers` *и* `DBUATCJ2:1452 → Identity.identity.AspNetUsers` |
| `leid.lei.address` | `DBUATCJ2:1450 → Thiqah.thi.Address` |
| **`LGD.lgd.LoanSecurity`** | **`DBUATCJ2:1450 → LGD.lgd.LoanSecurity`** ← точный матч schema+table |

**Особенно ценные:**

* **`LGD.lgd.LoanSecurity`** — в шите сервер «Not available», а реально она прямо доступна с VDI на DBUATCJ2:1450 с *тем же* `LGD.lgd.LoanSecurity` — **можно сразу резолвить, owner может закрыть строку**.
* **`Score_Result_Master`** — sybase'овская таблица, но **копия живёт на MSSQL** в `Consumer_SE.dbo`. Для DDL-генератора это даёт возможность получить колонки без Sybase.
* **`AspNetUsers`** — стандартная ASP.NET membership. Есть в 2 проектах: `CoreServicesDB.membership` (DBUATCJ2:1450) и `Identity.identity` (DBUATCJ2:1452 через tunnel). Какой именно соответствует `IdentityLei.dbo.AspNetUsers` — нужно уточнить у owner'а (по контексту скорее `Identity.identity`, потому что это identity-DB).
* **`leid.lei.address ≈ Thiqah.thi.Address`** — другая система (Thiqah), но имя совпадает. Возможно, та же сущность под другой системой; нужно подтверждение схемы.

### CLOSE-совпадения (имя похоже; локацию надо локализовать, баг в моей пробе)

Эти CLOSE-матчи показывают только имя — где именно живут эти таблицы, нужна follow-up проба. Самое интересное:

| Цель из шита | Близкое имя на сканированных серверах |
|---|---|
| `SIMAHDWH.PRS.PRODUCT` | `products`, `productmap` |
| **`SIMAHDWH.COM.COMACXA0`** | **`comacxm0`** — разница одна буква (`m` vs `a`)! Скорее всего опечатка в шите |
| `SIMAHDWH.SIMAH_DM.Commercial_Usage` | `commercial_usg_2` |
| `LEIPortal.dbo.PaymentStatusMaster` | `paymentstatus` |
| `leiportal.dbo.RequestStatus` | `adminrequeststatus`, `reportrequeststatus` |
| `Moarif.lei.Status` | `statuses`, `vatstatus` |
| **`SIMAH_UNIFIED.dbo.F_Com_Lei_Data_Loading_Stats`** | **`f_com_enquiries_data_loading_stats`, `f_con_enquiries_data_loading_stats`, `f_com_memberprofile_data_loading_stats`, `f_con_memberprofile_data_loading_stats`** ← консистентный паттерн `f_com/con_<thing>_data_loading_stats` существует! значит, аналогичные `*_Lei_*` и `*_Salary_Certificate_*` ДОЛЖНЫ быть где-то в той же БД |

→ **TODO:** перезапустить cross-DB hunt v2 с локализацией CLOSE-матчей, найти где живут `f_com/con_*_data_loading_stats` — это сразу даст owner'у SIMAH_UNIFIED БД, в которой нужно искать `F_Com_Lei_*` и `F_Con_Salary_Certificate_*`.

### MISS — нечего показать (~28 строк)

Ни exact, ни близкого. По бакетам:

* **`LINQ2SIMAH/_clone.MESSAGE_ARCHIVE / MESSAGE_OUT_BKP`** (4) — нигде, ждут реального доступа к DBSIMAHUAT1 (см. ниже про не-firewall)
* **Большинство `SIMAHDWH.STG/COM/PRS.*`** (~17) — легитимный MISS, они в Sybase, MSSQL не сканирует Sybase
* **`KSAPOC.NDP_Bulk / NDP_Score`** (2)
* **`leiportal.LeiRequest/Request/RequestAddress/...`** (5) — НЕ доступны на DBUATCJ2:1450 (хотя `leiportal` DB там есть!) — возможно, другая БД с тем же именем
* **`Moarif.lei.LeiRequest/RequestData`** (2)
* **`IdentityLei.dbo.User`**, **`leid.lei.lei`**, **`leid.lei.RequestHistory`** (3)
* **`SIMAH_UNIFIED.dbo.F_Con_Salary_Certificate_Data_Loading_Stats`** (1)

---

## 4. Ключевые новости / discoveries

### 🔴 DBSIMAHUAT1 — **не firewall, а login fail**

Ранее `DBSIMAHUAT1:1450` числился firewalled. Сегодня cross-DB hunt показал: **TCP проходит, сервер отвечает, `gpuatsrvusr` отвергнут с `42000 Login Failed`**. То есть **доступ к серверу есть**, нужен **другой логин/пароль**.

Это потенциально открывает 4 БД: **`Moarif`, `LEIPortal`, `LINQ2SIMAH`, `KSAPOC`** — то есть **~55 строк blank-Prerequisites** могут резолвнуться, если найти правильные creds для DBSIMAHUAT1.

→ **action:** запросить у owner'а DBSIMAHUAT1:1450 правильные creds.

### 🟡 Sybase SIMAHDWH — драйверы есть, creds не подходят

На VDI установлены **`DataDirect 8.0 Sybase Wire Protocol`** и **`DataDirect 7.1 Sybase Wire Protocol`**. С форматом `NetworkAddress=server,port` подключение **проходит до Sybase ASE**, и ASE отвечает: **`28000 Login Failed`**.

Пробовали два варианта пароля:
- `Si/19-80\@h` (литералом, 11 символов) — Login Failed
- `Si/19-80@h` (без `\`, 10 символов) — Login Failed

→ **action:** уточнить у owner'а правильный пароль для `GPUser1` на Sybase (либо подтвердить, что нужен другой логин). Если бы получилось — открыли бы **78 строк** (все SIMAHDWH).

### 🟢 EDWH grant работает, остаток — Sheet data quality

Из 71 EDWH-строк закрылось 10. Оставшиеся 61 — это **не проблема доступа**, а data quality в шите:

- 27 строк `STG_IDQ.*` — указан `Source DB = EDWH`, а реально это **другая БД** (`STG_IDQ` или отдельный staging-сервер)
- 9 строк `MPLT_*.*` — это **Informatica mapplet'ы, не SQL-таблицы**, нужно убрать из шита
- 25 строк `DM_IDQ/EDWH_CORE/IDQ.FCT_*` — таблицы **запланированы**, но **ещё не созданы** на EDWH (миграция не накатилась)

→ Полный детальный отчёт по EDWH-бакетам см. в transcript этой сессии (отдельный документ при необходимости).

### 🟢 LGD.LoanSecurity — ложный «not available»

В шите сервер помечен «Not available», а в реальности **`LGD.lgd.LoanSecurity` доступна на DBUATCJ2:1450** под нашим стандартным `gpuatsrvusr`. **Owner'у шита можно сразу поправить.**

### 🟢 INFA_MFT_STAGE — пароль другой

`INFUATHQSQL:1450 / INFA_MFT_STAGE` использует **`simah@123`**, не `Gp$r3viCc203345`. Сейчас в `secrets.local.json` зафиксировано.

### 🟢 ssh-bridge tunnel живёт стабильно

Из последнего цикла: 5 туннелей (`DBUATCJ2:1451/1452/1453`, `TRUAT01:1450`, `10.0.135.20:1433`) поднялись, отработали и были убиты. **Никаких следов на координаторе.**

---

## 5. Открытые блокеры и предлагаемые действия

| # | Блокер | Влияние | Кому | Действие |
|---:|---|---:|---|---|
| 1 | **SIMAH_UNIFIED login** `gpuatsrvusr` на DBMSTRUAT:1450 — `28000/18456` | 67 строк | DBA | reset / re-enable login; substate-проверить в SQL Server error log |
| 2 | **DBSIMAHUAT1:1450 login** — TCP проходит, `gpuatsrvusr` отбит | ~55 строк (Moarif/LEIPortal/LINQ2SIMAH/KSAPOC) | owner | дать правильные creds для DBSIMAHUAT1 (то что мы считали firewall'ом — это login) |
| 3 | **Sybase SIMAHDWH** — Login Failed обоими паролями | 78 строк | owner | уточнить пароль `GPUser1` или дать другой логин |
| 4 | **DEVDB01:1450** — firewall TCP (08001) | 3 строки (IdentityLei) | network team | реальный firewall-тикет (пока scope: DBSIMAHUAT1 уже сужается до login, может остаться один DEVDB01) |
| 5 | **EDWH USE denied** — остаток 61 | 61 строка | DBA + sheet owner | завершить grant + почистить data quality в шите (27 `STG_IDQ.*` + 9 `MPLT_*` + 25 «not created yet») |
| 6 | **NO_PROFILE** — SIMAHDQ/HUB/POOL и др. | 98 строк | owner | (уже частично закрыто этим циклом — новые creds попали) |

---

## 6. Что наработано для DDL-генератора прямо сейчас

Для **43 unready** таблиц из `ddl-generator/uc_not_ready_table_v1.md`:

* **4 таблицы** можно резолвить **немедленно** — найдены EXACT на доступных MSSQL:
  - `LGD.lgd.LoanSecurity` (sheet ошибочно «Not available»)
  - `SIMAHDWH.stg.Score_Result_Master` → `Consumer_SE.dbo` на DBUATCJ2:1450
  - `IdentityLei.dbo.AspNetUsers` → `CoreServicesDB.membership` или `Identity.identity`
  - `leid.lei.address` → `Thiqah.thi.Address` (нужно подтверждение что схема та же)
* **~6 таблиц** в категории CLOSE имеют сильные кандидаты (особенно `COMACXA0` ↔ `comacxm0` — почти точно опечатка). Нужна follow-up локализация.
* **2 таблицы** SIMAH_UNIFIED — у нас есть consistent pattern `f_com/con_*_data_loading_stats` в какой-то БД (надо локализовать → возможно та же БД содержит `F_Com_Lei_*` и `F_Con_Salary_*`).
* **~10 таблиц** заблокированы на DBSIMAHUAT1 login (если получим правильные creds — резолвится за один cycle).
* **~22 таблицы SIMAHDWH** заблокированы на Sybase login (если получим — резолвится через `find_sybase_unready_v3`).
* **~5 таблиц** действительно где-то ещё (нужны owner-ответы).

---

## 7. Что я сделаю дальше (предлагаю)

1. ☐ **find_unready_tables_v3** — фиксирую баг локализации CLOSE-матчей, прогон. Особенно для паттерна `f_com/con_*_data_loading_stats` — увидим в какой БД они живут.
2. ☐ **DBSIMAHUAT1 cred hunt** — когда дашь правильные creds, повторим cross-DB scan. Закроет ~55 строк.
3. ☐ **Sybase cred retry** — когда дашь правильный пароль, повторим find_sybase_unready_v1 + общий cycle. Закроет 78 строк.
4. ☐ Если хочешь — собрать **готовый текст письма owner'у шита** по EDWH data quality (27 + 9 + 25 строк) и по `LGD.LoanSecurity` (легитимная коррекция в `Source Server`).
