-- ApparelIQ ERP/WMS (SYNTHETIC) - read-only Fabric mirroring diagnostics (run via notebook 03_apply_sql_script).
SELECT name, is_change_feed_enabled FROM sys.databases WHERE name = DB_NAME();
GO
EXEC sys.sp_help_change_feed;
GO
SELECT TOP 20 session_id, error_number, error_message, entry_time FROM sys.dm_change_feed_errors ORDER BY entry_time DESC;
GO
SELECT TOP 20 * FROM sys.dm_change_feed_log_scan_sessions ORDER BY start_time DESC;
GO
