use std::{sync::Arc, time::Duration};

use base64::{Engine as _, engine::general_purpose::URL_SAFE_NO_PAD};
use centaur_sandbox_core::{SandboxError, SandboxId, SandboxSpec, SandboxStatus};
use centaur_session_sqlx::{PgSessionStore, SessionStoreError};
use rand::random;
use sha2::{Digest, Sha256};
use thiserror::Error;
use tokio::time::{MissedTickBehavior, interval};
use tracing::{info, warn};

use crate::SandboxManager;

pub type WarmSandboxSpecFactory = Arc<dyn Fn() -> SandboxSpec + Send + Sync>;
pub const SANDBOX_MODEL_TOKEN_ENV: &str = "CENTAUR_SANDBOX_MODEL_TOKEN";

pub struct WarmPoolConfig {
    pub target_size: usize,
    pub replenish_interval: Duration,
    pub bootstrap_iron_control_principal: Option<String>,
}

pub struct WarmPoolManager {
    manager: Arc<SandboxManager>,
    store: PgSessionStore,
    spec_factory: WarmSandboxSpecFactory,
    workload_key: String,
    config: WarmPoolConfig,
}

impl WarmPoolManager {
    pub fn new(
        manager: Arc<SandboxManager>,
        store: PgSessionStore,
        spec_factory: WarmSandboxSpecFactory,
        workload_key: impl Into<String>,
        config: WarmPoolConfig,
    ) -> Self {
        Self {
            manager,
            store,
            spec_factory,
            workload_key: workload_key.into(),
            config,
        }
    }

    pub fn workload_key(&self) -> &str {
        &self.workload_key
    }

    pub fn spawn_replenisher(self: Arc<Self>) {
        tokio::spawn(async move {
            let mut tick = interval(self.config.replenish_interval);
            tick.set_missed_tick_behavior(MissedTickBehavior::Delay);

            loop {
                // First `tick()` fires immediately, so this also runs on startup —
                // reaping warm sandboxes orphaned by a prior deploy before topping
                // up the current generation's pool.
                tick.tick().await;
                if let Err(error) = self.reconcile_stale_workload().await {
                    warn!(%error, "session sandbox warm pool stale-workload reconcile failed");
                }
                if let Err(error) = self.replenish_once().await {
                    warn!(%error, "session sandbox warm pool replenishment failed");
                }
            }
        });
    }

    /// Reap warm sandboxes left behind by previous deploy generations: any
    /// non-`claimed` warm sandbox whose `workload_key` differs from this
    /// generation's. A redeploy changes the sandbox image (hence `workload_key`),
    /// so the new generation otherwise never revisits the old key and those pods
    /// leak until the max-lifetime sweep. Stops each sandbox and removes its row.
    /// Returns the number reaped. Idempotent and safe to run repeatedly.
    pub async fn reconcile_stale_workload(&self) -> Result<usize, WarmPoolError> {
        let stale = self
            .store
            .list_reapable_stale_warm_sandboxes(self.workload_key.as_str())
            .await?;
        let mut reaped = 0usize;
        for sandbox_id in &stale {
            let id = SandboxId::new(sandbox_id.as_str());
            match self.manager.stop(&id).await {
                Ok(()) | Err(SandboxError::NotFound(_)) => {}
                Err(error) => {
                    warn!(%sandbox_id, %error, "warm pool: failed to stop stale warm sandbox");
                    continue;
                }
            }
            if let Err(error) = self.store.delete_warm_sandbox(sandbox_id).await {
                warn!(%sandbox_id, %error, "warm pool: failed to delete stale warm sandbox row");
                continue;
            }
            reaped += 1;
        }
        if reaped > 0 {
            info!(
                reaped,
                current_workload_key = %self.workload_key,
                "warm pool: reaped stale (previous-deploy) warm sandboxes"
            );
        }
        Ok(reaped)
    }

    pub async fn claim(
        &self,
        thread_key: &str,
        iron_control_principal: Option<&str>,
    ) -> Result<Option<String>, WarmPoolError> {
        loop {
            let Some(sandbox_id) = self
                .store
                .claim_ready_warm_sandbox(self.workload_key.as_str(), thread_key)
                .await?
            else {
                return Ok(None);
            };

            let id = SandboxId::new(sandbox_id.as_str());
            let failure = match self.manager.status(&id).await {
                // Only `Running` accepts `open_io`. `Created` means the
                // runtime regressed after the replenisher saw it running
                // (backends wait for readiness before returning from create),
                // so claiming it would fail at I/O attach.
                Ok(SandboxStatus::Running) => {
                    if let Some(principal_id) = iron_control_principal
                        && let Err(error) = self
                            .manager
                            .assign_iron_control_proxy_principal(&id, principal_id)
                            .await
                    {
                        let error_message = error.to_string();
                        let _ = self
                            .store
                            .mark_warm_sandbox_failed(&sandbox_id, &error_message)
                            .await;
                        return Err(WarmPoolError::Sandbox(error));
                    }
                    return Ok(Some(sandbox_id));
                }
                Ok(status) => format!("claimed warm sandbox was not running: {status:?}"),
                Err(SandboxError::NotFound(_)) => "claimed warm sandbox was not found".to_owned(),
                Err(error) => {
                    let error_message = error.to_string();
                    warn!(%sandbox_id, error = %error_message);
                    let _ = self
                        .store
                        .mark_warm_sandbox_failed(&sandbox_id, &error_message)
                        .await;
                    return Err(WarmPoolError::Sandbox(error));
                }
            };
            warn!(%sandbox_id, error = %failure, thread_key);
            self.store
                .mark_warm_sandbox_failed(&sandbox_id, &failure)
                .await?;
        }
    }

    async fn replenish_once(&self) -> Result<(), WarmPoolError> {
        let needed = self.config.target_size.saturating_sub(
            self.store
                .count_ready_warm_sandboxes(self.workload_key.as_str())
                .await?
                .max(0) as usize,
        );

        for _ in 0..needed {
            let mut spec = (self.spec_factory)();
            if let Some(principal_id) = &self.config.bootstrap_iron_control_principal {
                spec.iron_control_principal = Some(principal_id.clone());
            }
            let token = mint_sandbox_model_token();
            let token_hash = sandbox_model_token_hash(&token);
            spec = spec.env(SANDBOX_MODEL_TOKEN_ENV, token);
            let handle = self.manager.create_running(spec).await?;
            if let Err(error) = self
                .store
                .insert_ready_warm_sandbox(
                    handle.id.as_str(),
                    self.workload_key.as_str(),
                    Some(&token_hash),
                )
                .await
            {
                let _ = self.manager.stop(&handle.id).await;
                return Err(WarmPoolError::Store(error));
            }
        }

        Ok(())
    }
}

fn mint_sandbox_model_token() -> String {
    let bytes: [u8; 32] = random();
    URL_SAFE_NO_PAD.encode(bytes)
}

pub fn sandbox_model_token_hash(token: &str) -> String {
    hex::encode(Sha256::digest(token.as_bytes()))
}

#[cfg(test)]
mod tests {
    use std::{
        env,
        sync::{Arc, Mutex},
        time::{SystemTime, UNIX_EPOCH},
    };

    use async_trait::async_trait;
    use centaur_sandbox_core::{
        ObservedSandbox, SandboxBackend, SandboxError, SandboxHandle, SandboxId, SandboxIo,
        SandboxResult, SandboxSpec, SandboxStatus,
    };

    use super::*;

    #[tokio::test]
    async fn replenish_injects_model_token_and_stores_hash() {
        let Some(store) = test_store().await else {
            return;
        };
        let backend = Arc::new(FakeBackend::default());
        let manager = Arc::new(SandboxManager::new(backend.clone()));
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_nanos();
        let workload_key = format!("test-workload-{}-{nonce}", std::process::id());
        let warm_pool = WarmPoolManager::new(
            manager,
            store.clone(),
            Arc::new(|| SandboxSpec::new("mock")),
            workload_key,
            WarmPoolConfig {
                target_size: 1,
                replenish_interval: Duration::from_secs(60),
                bootstrap_iron_control_principal: None,
            },
        );

        warm_pool.replenish_once().await.expect("replenish");

        let specs = backend.created_specs();
        assert_eq!(specs.len(), 1);
        let token = specs[0]
            .env
            .iter()
            .find(|env| env.name == SANDBOX_MODEL_TOKEN_ENV)
            .map(|env| env.value.as_str())
            .expect("model token env");
        assert!(!token.is_empty());
        let record = store
            .find_warm_sandbox_by_token_hash(&sandbox_model_token_hash(token))
            .await
            .expect("lookup token hash")
            .expect("warm sandbox row");
        assert_eq!(record.sandbox_id, "fake-sbx-1");
        assert_eq!(record.status, "ready");
    }

    async fn test_store() -> Option<PgSessionStore> {
        let Ok(url) = env::var("SESSION_SQLX_TEST_DATABASE_URL")
            .or_else(|_| env::var("SESSION_RUNTIME_TEST_DATABASE_URL"))
        else {
            return None;
        };
        let store = PgSessionStore::connect(&url)
            .await
            .expect("connect test db");
        store.run_migrations().await.expect("run migrations");
        Some(store)
    }

    #[derive(Default)]
    struct FakeBackend {
        created: Mutex<Vec<SandboxSpec>>,
    }

    impl FakeBackend {
        fn created_specs(&self) -> Vec<SandboxSpec> {
            self.created.lock().unwrap().clone()
        }
    }

    #[async_trait]
    impl SandboxBackend for FakeBackend {
        fn name(&self) -> &'static str {
            "fake"
        }

        async fn create(&self, spec: SandboxSpec) -> SandboxResult<SandboxHandle> {
            let mut created = self.created.lock().unwrap();
            created.push(spec);
            Ok(SandboxHandle::new(
                format!("fake-sbx-{}", created.len()),
                self.name(),
            ))
        }

        async fn open_io(&self, _id: &SandboxId) -> SandboxResult<SandboxIo> {
            Err(SandboxError::Unsupported {
                backend: self.name(),
                operation: "open_io",
            })
        }

        async fn status(&self, _id: &SandboxId) -> SandboxResult<SandboxStatus> {
            Ok(SandboxStatus::Running)
        }

        async fn observe(&self, id: &SandboxId) -> SandboxResult<ObservedSandbox> {
            Ok(ObservedSandbox::new(
                id.as_str(),
                self.name(),
                SandboxStatus::Running,
            ))
        }

        async fn list_observed(&self) -> SandboxResult<Vec<ObservedSandbox>> {
            Ok(Vec::new())
        }

        async fn stop(&self, _id: &SandboxId) -> SandboxResult<()> {
            Ok(())
        }

        async fn pause(&self, _id: &SandboxId) -> SandboxResult<()> {
            Ok(())
        }

        async fn resume(&self, _id: &SandboxId) -> SandboxResult<()> {
            Ok(())
        }
    }
}

#[derive(Debug, Error)]
pub enum WarmPoolError {
    #[error(transparent)]
    Store(#[from] SessionStoreError),
    #[error(transparent)]
    Sandbox(#[from] SandboxError),
}
