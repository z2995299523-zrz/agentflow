-- 对公存款账户日快照
-- 来源：ods.core_dep_acct 关联科目表取科目类别，关联汇率表折人民币
-- 范围：只保留对公客户的正常账户
-- 按数据日期先删后插，可重复执行

DELETE FROM dwd.corp_dep_acct_d WHERE data_dt = DATE '${data_dt}';

INSERT INTO dwd.corp_dep_acct_d
    (data_dt, acct_no, cust_no, org_no, subj_no, subj_type_cd, is_margin, ccy_cd, acct_bal, bal_rmb)
SELECT a.data_dt,
       a.acct_no,
       a.cust_no,
       a.org_no,
       a.subj_no,
       s.subj_type_cd,
       CASE WHEN s.subj_type_cd = '03' THEN '1' ELSE '0' END AS is_margin,
       a.ccy_cd,
       a.acct_bal,
       ROUND(a.acct_bal * r.mid_rate, 2) AS bal_rmb
FROM ods.core_dep_acct a
JOIN ods.gl_subject s ON a.subj_no = s.subj_no
JOIN ods.exch_rate r ON a.ccy_cd = r.ccy_cd AND a.data_dt = r.data_dt
WHERE a.data_dt = DATE '${data_dt}'
  AND a.cust_type = '1'
  AND a.acct_stat = '0';
