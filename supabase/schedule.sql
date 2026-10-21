-- Enable pg_cron and pg_net in Supabase Extensions first.
-- In Vault, add household_url (https://your-production-domain.vercel.app)
-- and household_cron_secret (same value as Vercel CRON_SECRET).
SELECT cron.schedule('mom-household-process', '* * * * *', $$
 SELECT net.http_post(
   url := (SELECT decrypted_secret FROM vault.decrypted_secrets WHERE name='household_url') || '/api/process',
   headers := jsonb_build_object('Content-Type', 'application/json', 'Authorization',
     'Bearer ' || (SELECT decrypted_secret FROM vault.decrypted_secrets WHERE name='household_cron_secret')),
   body := '{}'::jsonb,
   timeout_milliseconds := 240000
 );
$$);
