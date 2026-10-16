-- 对公贷款余额类样例
-- 专题层的贷款汇总表只有一张：按客户和放款机构汇总的 ads.corp_cust_loan_bal_d，不良贷款余额是其中的 npl_bal_rmb。

-- 需求: 2026 年 7 月末不良贷款最多的 5 家企业，要企业名称和不良余额
-- 口径: 不良贷款余额用 npl_bal_rmb（折人民币）。同一客户可能在多个机构放款，要先按客户汇总再排序取前 5；没有不良的客户不进榜
SELECT c.cust_nm,
       SUM(l.npl_bal_rmb) AS npl_bal_rmb
FROM ads.corp_cust_loan_bal_d l
JOIN dim.corp_cust c ON l.cust_no = c.cust_no
WHERE l.data_dt = DATE '2026-07-31'
GROUP BY l.cust_no, c.cust_nm
HAVING SUM(l.npl_bal_rmb) > 0
ORDER BY npl_bal_rmb DESC
LIMIT 5;

-- 需求: 2026 年 6 月末一级分行的不良贷款余额和不良率，百分数精确到小数点后两位
-- 口径: 不良率 = 不良贷款余额 ÷ 贷款余额；先按一级分行汇总两个余额，再相除，分母为零时不算比率
SELECT o.lvl1_org_nm,
       SUM(l.npl_bal_rmb) AS npl_bal_rmb,
       ROUND(100.0 * SUM(l.npl_bal_rmb) / NULLIF(SUM(l.loan_bal_rmb), 0), 2) AS npl_ratio_pct
FROM ads.corp_cust_loan_bal_d l
JOIN dim.org o ON l.org_no = o.org_no
WHERE l.data_dt = DATE '2026-06-30'
GROUP BY o.lvl1_org_nm;

-- 需求: 2026 年 4 月末同一机构既有存款又有贷款的客户，按行业汇总存款和贷款余额
-- 口径: 存款用全口径（含保证金）。存款表和贷款表是一对一关联，要用日期、客户号、机构号三个键一起关联；只统计在同一机构两边余额都大于零的客户
SELECT c.industry_nm,
       SUM(d.dep_bal_tot_rmb) AS dep_bal_tot_rmb,
       SUM(l.loan_bal_rmb) AS loan_bal_rmb
FROM ads.corp_cust_dep_bal_d d
JOIN ads.corp_cust_loan_bal_d l
  ON d.data_dt = l.data_dt AND d.cust_no = l.cust_no AND d.org_no = l.org_no
JOIN dim.corp_cust c ON d.cust_no = c.cust_no
WHERE d.data_dt = DATE '2026-04-30'
  AND d.dep_bal_tot_rmb > 0
  AND l.loan_bal_rmb > 0
GROUP BY c.industry_nm;
