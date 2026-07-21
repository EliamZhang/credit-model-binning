SELECT
    -- ID
    user_id,
    application_id,

    -- 时间
    application_time,
    application_date,
    application_month,

    -- 状态（漏斗用）
    status,
    application_status,
    assessment_status,

    -- 负债比
    LTI,
    PTI,
    NSTI,

    -- 本金
    principal,

    -- 剩余本金
    estimate_principal_remaining_mob0,
    estimate_principal_remaining_mob1,
    estimate_principal_remaining_mob2,
    estimate_principal_remaining_mob3,
    estimate_principal_remaining_mob4,

    -- DPD
    dpd_days_ever_mob0,
    dpd_days_ever_mob1,
    dpd_days_ever_mob2,
    dpd_days_ever_mob3,
    dpd_days_ever_mob4,

    -- 笔数逾期标签
    duedate_1m_5,
    duedate_2m_5,
    duedate_3m_5,
    duedate_1m_30,
    duedate_2m_30,
    duedate_3m_30,
    duedate_4m_30,

    -- 标签
    application_tag,
    user_tag,
    loan_tag

FROM ba.customer_profile_rawdata
WHERE application_time >= '2024-01-01';
