-- 对公客户存款余额日汇总
-- 来源：dwd.corp_dep_acct_d，按客户和开户机构汇总
-- 范围：关联客户维表，剔除客户号在客户维表里不存在的记录
-- 日均：年初（或月初）至当日每日余额之和除以天数，不含保证金
-- 按数据日期先删后插，可重复执行

DELETE FROM ads.corp_cust_dep_bal_d WHERE data_dt = DATE '${data_dt}';

INSERT INTO ads.corp_cust_dep_bal_d
    (data_dt, cust_no, org_no, dep_bal_rmb, margin_bal_rmb, dep_bal_tot_rmb,
     cur_dep_bal_rmb, fix_dep_bal_rmb, ytd_avg_bal_rmb, mtd_avg_bal_rmb)
WITH cur AS (
    SELECT a.data_dt,
           a.cust_no,
           a.org_no,
           SUM(CASE WHEN a.is_margin = '0' THEN a.bal_rmb ELSE 0 END) AS dep_bal_rmb,
           SUM(CASE WHEN a.is_margin = '1' THEN a.bal_rmb ELSE 0 END) AS margin_bal_rmb,
           SUM(a.bal_rmb) AS dep_bal_tot_rmb,
           SUM(CASE WHEN a.subj_type_cd = '01' THEN a.bal_rmb ELSE 0 END) AS cur_dep_bal_rmb,
           SUM(CASE WHEN a.subj_type_cd = '02' THEN a.bal_rmb ELSE 0 END) AS fix_dep_bal_rmb
    FROM dwd.corp_dep_acct_d a
    JOIN dim.corp_cust c ON a.cust_no = c.cust_no
    WHERE a.data_dt = DATE '${data_dt}'
    GROUP BY a.data_dt, a.cust_no, a.org_no
),
ytd AS (
    SELECT a.cust_no,
           a.org_no,
           SUM(CASE WHEN a.is_margin = '0' THEN a.bal_rmb ELSE 0 END) AS acm_bal_rmb
    FROM dwd.corp_dep_acct_d a
    WHERE a.data_dt BETWEEN CAST(date_trunc('year', DATE '${data_dt}') AS DATE) AND DATE '${data_dt}'
    GROUP BY a.cust_no, a.org_no
),
mtd AS (
    SELECT a.cust_no,
           a.org_no,
           SUM(CASE WHEN a.is_margin = '0' THEN a.bal_rmb ELSE 0 END) AS acm_bal_rmb
    FROM dwd.corp_dep_acct_d a
    WHERE a.data_dt BETWEEN CAST(date_trunc('month', DATE '${data_dt}') AS DATE) AND DATE '${data_dt}'
    GROUP BY a.cust_no, a.org_no
)
SELECT cur.data_dt,
       cur.cust_no,
       cur.org_no,
       cur.dep_bal_rmb,
       cur.margin_bal_rmb,
       cur.dep_bal_tot_rmb,
       cur.cur_dep_bal_rmb,
       cur.fix_dep_bal_rmb,
       ROUND(ytd.acm_bal_rmb / (DATE '${data_dt}' - CAST(date_trunc('year', DATE '${data_dt}') AS DATE) + 1), 2) AS ytd_avg_bal_rmb,
       ROUND(mtd.acm_bal_rmb / (DATE '${data_dt}' - CAST(date_trunc('month', DATE '${data_dt}') AS DATE) + 1), 2) AS mtd_avg_bal_rmb
FROM cur
JOIN ytd ON cur.cust_no = ytd.cust_no AND cur.org_no = ytd.org_no
JOIN mtd ON cur.cust_no = mtd.cust_no AND cur.org_no = mtd.org_no;
