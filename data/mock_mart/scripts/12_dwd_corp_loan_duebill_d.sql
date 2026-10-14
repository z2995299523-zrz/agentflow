-- 对公贷款借据日快照
-- 来源：ods.core_loan_duebill 关联汇率表折人民币
-- 范围：只保留对公客户、余额大于零的借据
-- 不良：五级分类为次级、可疑、损失
-- 按数据日期先删后插，可重复执行

DELETE FROM dwd.corp_loan_duebill_d WHERE data_dt = DATE '${data_dt}';

INSERT INTO dwd.corp_loan_duebill_d
    (data_dt, duebill_no, cust_no, org_no, prod_cd, ccy_cd, loan_bal, bal_rmb, five_cls_cd, is_npl)
SELECT l.data_dt,
       l.duebill_no,
       l.cust_no,
       l.org_no,
       l.prod_cd,
       l.ccy_cd,
       l.loan_bal,
       ROUND(l.loan_bal * r.mid_rate, 2) AS bal_rmb,
       l.five_cls_cd,
       CASE WHEN l.five_cls_cd IN ('3', '4', '5') THEN '1' ELSE '0' END AS is_npl
FROM ods.core_loan_duebill l
JOIN ods.exch_rate r ON l.ccy_cd = r.ccy_cd AND l.data_dt = r.data_dt
WHERE l.data_dt = DATE '${data_dt}'
  AND l.cust_type = '1'
  AND l.loan_bal > 0;
