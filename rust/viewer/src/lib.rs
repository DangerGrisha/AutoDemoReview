use bevy::prelude::*;
use wasm_bindgen::prelude::*;

// Hooks the HTML page defines on `window` so the page can measure
// time-to-first-frame and prove the render loop is advancing.
#[wasm_bindgen]
extern "C" {
    #[wasm_bindgen(js_namespace = window, js_name = bevyFirstFrame)]
    fn bevy_first_frame();
    #[wasm_bindgen(js_namespace = window, js_name = bevyFrameTick)]
    fn bevy_frame_tick(n: u32);
}

#[derive(Component)]
struct Spinner;

#[wasm_bindgen]
pub fn run(canvas_selector: String) {
    console_error_panic_hook::set_once();
    App::new()
        .add_plugins(DefaultPlugins.set(WindowPlugin {
            primary_window: Some(Window {
                canvas: Some(canvas_selector),
                fit_canvas_to_parent: true,
                prevent_default_event_handling: false,
                ..default()
            }),
            ..default()
        }))
        .add_systems(Startup, setup)
        .add_systems(Update, (spin, report_frames))
        .run();
}

fn setup(
    mut commands: Commands,
    mut meshes: ResMut<Assets<Mesh>>,
    mut materials: ResMut<Assets<StandardMaterial>>,
) {
    commands.spawn((
        Mesh3d(meshes.add(Cuboid::new(1.5, 1.5, 1.5))),
        MeshMaterial3d(materials.add(StandardMaterial {
            base_color: Color::srgb(0.9, 0.5, 0.1),
            ..default()
        })),
        Transform::default(),
        Spinner,
    ));
    commands.spawn((
        DirectionalLight {
            illuminance: 8_000.0,
            ..default()
        },
        Transform::from_xyz(3.0, 5.0, 3.0).looking_at(Vec3::ZERO, Vec3::Y),
    ));
    commands.spawn((
        Camera3d::default(),
        Transform::from_xyz(3.0, 2.5, 4.0).looking_at(Vec3::ZERO, Vec3::Y),
    ));
}

fn spin(time: Res<Time>, mut query: Query<&mut Transform, With<Spinner>>) {
    for mut transform in &mut query {
        transform.rotate_y(0.9 * time.delta_secs());
        transform.rotate_x(0.35 * time.delta_secs());
    }
}

fn report_frames(mut count: Local<u32>) {
    *count += 1;
    if *count == 1 {
        bevy_first_frame();
    }
    bevy_frame_tick(*count);
}
