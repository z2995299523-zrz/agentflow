-- 对公客户维表：客户基本信息加上行业名称和规模名称
-- 来源：ods.ecif_corp_cust 关联 ods.industry_dict
-- 全量刷新

TRUNCATE TABLE dim.corp_cust;

INSERT INTO dim.corp_cust (cust_no, cust_nm, industry_cd, industry_nm, scale_cd, scale_nm)
SELECT c.cust_no,
       c.cust_nm,
       c.industry_cd,
       i.industry_nm,
       c.scale_cd,
       CASE c.scale_cd WHEN '1' THEN '大型' WHEN '2' THEN '中型' WHEN '3' THEN '小型' WHEN '4' THEN '微型' END AS scale_nm
FROM ods.ecif_corp_cust c
LEFT JOIN ods.industry_dict i ON c.industry_cd = i.industry_cd;
