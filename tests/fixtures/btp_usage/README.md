# BTP Usage Data Management fixtures

Recorded 2026-11-27 from the Usage Data Management service (`uas`, plan `reporting-ga-admin`),
Reporting API `GET /reports/v1/monthlySubaccountsCost?fromDate=202609&toDate=202611` and
`GET /reports/v1/monthlyUsage?fromDate=202609&toDate=202611` (`fromDate` / `toDate` as `YYYYMM`).
Structure, field names, field order and value types are as SAP answered; a few rows are kept.
Every id and name (global account, directory, subaccount, identity zone, instance) is a
placeholder, and every amount and quantity (`usage`, `cost`, `cloudCreditsCost`, ...) is
synthetic, as is `crmSku`. SAP's own names (`serviceId`, `plan`, `measureId`, model names in
`application`) are real. `subaccount-a` holds the AI Core rows, `subaccount-b` is another
subaccount, and the row with an empty `subaccountId` is a global-account-level service.
