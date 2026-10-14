-- 对公客户贷款余额日汇总
-- 来源：dwd.corp_loan_duebill_d，按客户和放款机构汇总
-- 范围：关联客户维表，剔除客户号在客户维表里不存在的记录
-- 按数据日期先删后插，可重复执行

DROP TABLE IF EXISTS tmp_corp_loan;

CREATE TEMPORARY TABLE tmp_corp_loan AS
SELECT l.data_dt,
       l.cust_no,
       l.org_no,
       l.duebill_no,
       l.bal_rmb,
       l.is_npl
FROM dwd.corp_loan_duebill_d l
JOIN dim.corp_cust c ON l.cust_no = c.cust_no
WHERE l.data_dt = DATE '${data_dt}';

DELETE FROM ads.corp_cust_loan_bal_d WHERE data_dt = DATE '${data_dt}';

INSERT INTO ads.corp_cust_loan_bal_d
    (data_dt, cust_no, org_no, loan_bal_rmb, npl_bal_rmb, duebill_cnt)
SELECT t.data_dt,
       t.cust_no,
       t.org_no,
       SUM(t.bal_rmb) AS loan_bal_rmb,
       SUM(CASE WHEN t.is_npl = '1' THEN t.bal_rmb ELSE 0 END) AS npl_bal_rmb,
       COUNT(t.duebill_no) AS duebill_cnt
FROM tmp_corp_loan t
GROUP BY t.data_dt, t.cust_no, t.org_no;

DROP TABLE tmp_corp_loan;
