use gpui::Context;
use settings::SettingsStore;
use std::time::Duration;
use ui::App;

pub struct BlinkManager {
    blink_interval: Duration,
    blink_epoch: usize,
    /// Whether the blinking is paused.
    blinking_paused: bool,
    /// Whether the cursor should be visibly rendered or not.
    visible: bool,
    /// Whether the blinking is currently enabled.
    enabled: bool,
    /// Whether the blinking is enabled in the settings.
    blink_enabled_in_settings: fn(&App) -> bool,
    last_blink_enabled_in_settings: bool,
}

impl BlinkManager {
    pub fn new(
        blink_interval: Duration,
        blink_enabled_in_settings: fn(&App) -> bool,
        cx: &mut Context<Self>,
    ) -> Self {
        let last_blink_enabled_in_settings = blink_enabled_in_settings(cx);
        // Unrelated settings updates (including remote worktree settings) must
        // not toggle the cursor or reset its blink deadline.
        cx.observe_global::<SettingsStore>(move |this, cx| {
            let enabled = (this.blink_enabled_in_settings)(cx);
            if enabled != this.last_blink_enabled_in_settings {
                this.last_blink_enabled_in_settings = enabled;
                this.pause_blinking(cx);
            }
        })
        .detach();

        Self {
            blink_interval,
            blink_epoch: 0,
            blinking_paused: false,
            visible: true,
            enabled: false,
            blink_enabled_in_settings,
            last_blink_enabled_in_settings,
        }
    }

    fn next_blink_epoch(&mut self) -> usize {
        self.blink_epoch += 1;
        self.blink_epoch
    }

    pub fn pause_blinking(&mut self, cx: &mut Context<Self>) {
        self.show_cursor(cx);
        self.blinking_paused = true;

        let epoch = self.next_blink_epoch();
        let interval = Duration::from_millis(500);
        cx.spawn(async move |this, cx| {
            cx.background_executor().timer(interval).await;
            this.update(cx, |this, cx| this.resume_cursor_blinking(epoch, cx))
        })
        .detach();
    }

    fn resume_cursor_blinking(&mut self, epoch: usize, cx: &mut Context<Self>) {
        if epoch == self.blink_epoch {
            self.blinking_paused = false;
            self.blink_cursors(epoch, cx);
        }
    }

    fn blink_cursors(&mut self, epoch: usize, cx: &mut Context<Self>) {
        if (self.blink_enabled_in_settings)(cx) {
            if epoch == self.blink_epoch && self.enabled && !self.blinking_paused {
                self.visible = !self.visible;
                cx.notify();

                let epoch = self.next_blink_epoch();
                let interval = self.blink_interval;
                cx.spawn(async move |this, cx| {
                    cx.background_executor().timer(interval).await;
                    if let Some(this) = this.upgrade() {
                        this.update(cx, |this, cx| this.blink_cursors(epoch, cx));
                    }
                })
                .detach();
            }
        } else {
            self.show_cursor(cx);
        }
    }

    pub fn show_cursor(&mut self, cx: &mut Context<BlinkManager>) {
        if !self.visible {
            self.visible = true;
            cx.notify();
        }
    }

    /// Enable the blinking of the cursor.
    pub fn enable(&mut self, cx: &mut Context<Self>) {
        if self.enabled {
            return;
        }

        self.enabled = true;
        self.blinking_paused = false;
        // Set cursors as invisible and start blinking: this causes cursors
        // to be visible during the next render.
        self.visible = false;
        self.blink_cursors(self.blink_epoch, cx);
    }

    /// Disable the blinking of the cursor.
    pub fn disable(&mut self, _cx: &mut Context<Self>) {
        self.visible = false;
        self.enabled = false;
    }

    pub fn visible(&self) -> bool {
        self.visible
    }

    #[cfg(test)]
    pub(crate) fn enabled(&self) -> bool {
        self.enabled
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use gpui::{AppContext, BorrowAppContext, Entity, Global, TestAppContext};

    struct BlinkSetting(bool);
    impl Global for BlinkSetting {}

    fn manager(cx: &mut TestAppContext) -> Entity<BlinkManager> {
        cx.update(|cx| {
            let settings = SettingsStore::test(cx);
            cx.set_global(settings);
            cx.set_global(BlinkSetting(true));
            let manager = cx.new(|cx| {
                BlinkManager::new(
                    Duration::from_millis(500),
                    |cx| cx.global::<BlinkSetting>().0,
                    cx,
                )
            });
            manager.update(cx, BlinkManager::enable);
            manager
        })
    }

    fn advance(cx: &mut TestAppContext, millis: u64) {
        cx.run_until_parked();
        cx.executor().advance_clock(Duration::from_millis(millis));
        cx.run_until_parked();
    }

    fn refresh_settings(cx: &mut TestAppContext, enabled: bool) {
        cx.update(|cx| {
            cx.set_global(BlinkSetting(enabled));
            cx.update_global::<SettingsStore, _>(|_, _| {});
        });
        cx.run_until_parked();
    }

    #[gpui::test]
    fn unrelated_settings_refreshes_preserve_blink_deadline(cx: &mut TestAppContext) {
        let manager = manager(cx);
        for _ in 0..4 {
            advance(cx, 100);
            refresh_settings(cx, true);
            assert!(manager.read_with(cx, |manager, _| manager.visible()));
        }
        advance(cx, 100);
        assert!(!manager.read_with(cx, |manager, _| manager.visible()));
    }

    #[gpui::test]
    fn typing_keeps_cursor_visible_through_settings_refreshes(cx: &mut TestAppContext) {
        let manager = manager(cx);
        advance(cx, 400);
        manager.update(cx, BlinkManager::pause_blinking);
        for _ in 0..4 {
            advance(cx, 100);
            refresh_settings(cx, true);
            assert!(manager.read_with(cx, |manager, _| manager.visible()));
        }
        advance(cx, 100);
        assert!(!manager.read_with(cx, |manager, _| manager.visible()));
    }

    #[gpui::test]
    fn changing_cursor_blink_setting_stops_and_restarts_blinking(cx: &mut TestAppContext) {
        let manager = manager(cx);
        advance(cx, 500);
        assert!(!manager.read_with(cx, |manager, _| manager.visible()));
        refresh_settings(cx, false);
        assert!(manager.read_with(cx, |manager, _| manager.visible()));
        advance(cx, 1000);
        assert!(manager.read_with(cx, |manager, _| manager.visible()));
        refresh_settings(cx, true);
        advance(cx, 499);
        assert!(manager.read_with(cx, |manager, _| manager.visible()));
        advance(cx, 1);
        assert!(!manager.read_with(cx, |manager, _| manager.visible()));
    }
}
