-- 上溯到标准层的样例
-- 专题层没有的维度（币种、贷款产品）和明细，要上溯到标准层 dwd。
-- 专题层的加工脚本会剔除客户号在客户维表里不存在的记录，上溯后想和专题层的数对得上，也要关联 dim.corp_cust。

-- 需求: 2026 年 6 月末对公存款账户按币种统计账户数和折人民币余额
-- 口径: 上溯到对公存款账户日快照，含保证金；关联客户维表是为了和专题层的客户范围一致，否则会多出无效客户的账户
SELECT a.ccy_cd,
       COUNT(a.acct_no) AS acct_cnt,
       SUM(a.bal_rmb) AS bal_rmb
FROM dwd.corp_dep_acct_d a
JOIN dim.corp_cust c ON a.cust_no = c.cust_no
WHERE a.data_dt = DATE '2026-06-30'
GROUP BY a.ccy_cd;

-- 需求: 2026 年 8 月末对公不良贷款在各个贷款产品上的分布，折人民币
-- 口径: 上溯到对公贷款借据日快照；不良用借据上的 is_npl = '1'（五级分类为次级、可疑、损失），同样要关联客户维表，保证和专题层的客户范围一致
SELECT l.prod_cd,
       SUM(l.bal_rmb) AS npl_bal_rmb
FROM dwd.corp_loan_duebill_d l
JOIN dim.corp_cust c ON l.cust_no = c.cust_no
WHERE l.data_dt = DATE '2026-08-31'
  AND l.is_npl = '1'
GROUP BY l.prod_cd;

-- 需求: 2026 年 6 月末单笔余额超过 2000 万元的对公贷款借据明细，按余额从大到小排列
-- 口径: 单笔余额用折人民币余额 bal_rmb，2000 万元写成 20000000；借据已经是明细粒度，不需要汇总
SELECT duebill_no,
       cust_no,
       prod_cd,
       bal_rmb
FROM dwd.corp_loan_duebill_d
WHERE data_dt = DATE '2026-06-30'
  AND bal_rmb > 20000000
ORDER BY bal_rmb DESC;
