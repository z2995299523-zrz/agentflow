-- 机构对公存款余额日汇总
-- 来源：ads.corp_cust_dep_bal_d，按开户机构汇总
-- 有余额客户数：全口径存款余额大于零的客户数，在本机构内去重
-- 按数据日期先删后插，可重复执行

DELETE FROM ads.org_corp_dep_bal_d WHERE data_dt = DATE '${data_dt}';

INSERT INTO ads.org_corp_dep_bal_d
    (data_dt, org_no, dep_bal_rmb, margin_bal_rmb, dep_bal_tot_rmb, ytd_avg_bal_rmb, dep_cust_cnt)
SELECT b.data_dt,
       b.org_no,
       SUM(b.dep_bal_rmb) AS dep_bal_rmb,
       SUM(b.margin_bal_rmb) AS margin_bal_rmb,
       SUM(b.dep_bal_tot_rmb) AS dep_bal_tot_rmb,
       SUM(b.ytd_avg_bal_rmb) AS ytd_avg_bal_rmb,
       COUNT(DISTINCT CASE WHEN b.dep_bal_tot_rmb > 0 THEN b.cust_no END) AS dep_cust_cnt
FROM ads.corp_cust_dep_bal_d b
WHERE b.data_dt = DATE '${data_dt}'
GROUP BY b.data_dt, b.org_no;
