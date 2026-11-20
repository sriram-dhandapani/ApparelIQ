-- ApparelIQ ERP/WMS (SYNTHETIC) - least-privilege principal for Fabric mirroring.
-- Placeholders are substituted by databricks/notebooks/03_apply_sql_script (never hard-code environment ids here):
--   {{FABRIC_PRINCIPAL_NAME}}    display name of the Entra principal used by the Fabric mirroring connection
--   {{FABRIC_PRINCIPAL_APP_ID}}  its Entra application (client) id
-- SID + TYPE = E maps the user to the app id directly, so the SQL server identity needs no Directory Readers
-- (FROM EXTERNAL PROVIDER fails with "Server identity does not have the Microsoft Entra Directory Readers permission").

IF NOT EXISTS (SELECT 1 FROM sys.database_principals WHERE name = N'{{FABRIC_PRINCIPAL_NAME}}')
BEGIN
    DECLARE @sid nvarchar(64) = CONVERT(nvarchar(64), CONVERT(varbinary(16), CAST('{{FABRIC_PRINCIPAL_APP_ID}}' AS uniqueidentifier)), 1);
    EXEC(N'CREATE USER [{{FABRIC_PRINCIPAL_NAME}}] WITH SID = ' + @sid + N', TYPE = E;');
END
GO

-- Permissions documented for Azure SQL Database mirroring in Fabric (read + change feed control only).
GRANT SELECT, ALTER ANY EXTERNAL MIRROR, VIEW DATABASE PERFORMANCE STATE, VIEW DATABASE SECURITY STATE
    TO [{{FABRIC_PRINCIPAL_NAME}}];
GO

-- The mirror only reads; agent write-back (erp.allocation_decision / erp.stock_transfer_order) uses a separate principal.
DENY INSERT, UPDATE, DELETE, EXECUTE TO [{{FABRIC_PRINCIPAL_NAME}}];
GO
