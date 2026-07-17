---
name: sql-application-extract
description: SQL file that extracts application info from ba.customer_profile_rawdata
metadata:
  type: reference
---

[scr/application_info_extract.sql](scr/application_info_extract.sql) 从 `ba.customer_profile_rawdata` 表提取申请信息，包含用户ID、申请ID、申请时间、放款日期、LTI/PTI/NSTI、各 MOB 的本金余额和逾期待还天数、首期还款信息、宽限期字段、标签字段、金额和期限字段等。筛选条件为 `application_time >= '2025-01-01'`。
