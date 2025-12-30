//! Smart crop the icon using Gemini vision to find the cat.
//!
//! Analyzes the current icon, finds the cat's bounding box, and crops tighter.
//!
//! # Usage
//!
//! ```bash
//! GEMINI_API_KEY=your_key cargo run --bin smart-crop
//! ```

use base64::Engine;
use image::imageops::FilterType;
use image::ImageFormat;
use rust_genai::{Client, InteractionStatus};
use std::env;
use std::io::Cursor;
use std::path::PathBuf;

fn get_assets_dir() -> PathBuf {
    let manifest_dir = env!("CARGO_MANIFEST_DIR");
    PathBuf::from(manifest_dir)
        .parent()
        .unwrap()
        .parent()
        .unwrap()
        .join("assets")
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let api_key = env::var("GEMINI_API_KEY").expect("GEMINI_API_KEY environment variable not set");
    let client = Client::builder(api_key).build();

    let assets_dir = get_assets_dir();
    let icon_path = assets_dir.join("icon.png");

    println!("=== SMART CROP ===\n");
    println!("Loading: {}\n", icon_path.display());

    // Load and encode current icon
    let icon_bytes = std::fs::read(&icon_path)?;
    let icon_base64 = base64::engine::general_purpose::STANDARD.encode(&icon_bytes);

    // Ask Gemini for bounding box
    println!("Analyzing image to find cat bounds...\n");

    let prompt = r#"Look at this image and find the cat (including the yarn ball it's playing with).

Return ONLY the bounding box coordinates as four integers separated by commas: left,top,right,bottom

The coordinates should be pixel values relative to the image dimensions.
Include some padding around the cat (about 5-10% of the crop size).

Example response: 150,80,620,550

Do not include any other text, just the four numbers."#;

    let response = client
        .interaction()
        .with_model("gemini-3-flash-preview")
        .with_text(prompt)
        .add_image_data(&icon_base64, "image/png")
        .with_store(true)
        .create()
        .await?;

    if response.status != InteractionStatus::Completed {
        return Err(format!("Interaction failed: {:?}", response.status).into());
    }

    let text = response.text().ok_or("No text response")?;
    println!("Gemini response: {}\n", text);

    // Parse bounding box
    let coords: Vec<u32> = text
        .trim()
        .split(',')
        .filter_map(|s| s.trim().parse().ok())
        .collect();

    if coords.len() != 4 {
        return Err(format!("Expected 4 coordinates, got: {:?}", coords).into());
    }

    let (left, top, right, bottom) = (coords[0], coords[1], coords[2], coords[3]);
    println!("Bounding box: left={}, top={}, right={}, bottom={}", left, top, right, bottom);

    // Load image and crop
    let img = image::load_from_memory(&icon_bytes)?;
    println!("Original size: {}x{}", img.width(), img.height());

    // Make it square (use the larger dimension)
    let crop_width = right - left;
    let crop_height = bottom - top;
    let size = crop_width.max(crop_height);

    // Center the square crop
    let center_x = (left + right) / 2;
    let center_y = (top + bottom) / 2;
    let half = size / 2;

    let crop_left = center_x.saturating_sub(half);
    let crop_top = center_y.saturating_sub(half);

    println!("Cropping: {}x{} from ({}, {})", size, size, crop_left, crop_top);

    let cropped = img.crop_imm(crop_left, crop_top, size, size);

    // Save at different sizes
    for target_size in [512u32, 1024u32] {
        let resized = cropped.resize(target_size, target_size, FilterType::Lanczos3);
        let path = assets_dir.join(format!("icon-{}.png", target_size));

        let mut buf = Cursor::new(Vec::new());
        resized.write_to(&mut buf, ImageFormat::Png)?;
        std::fs::write(&path, buf.into_inner())?;

        println!("Saved: {}", path.display());
    }

    // Save cropped original
    let path = assets_dir.join("icon.png");
    let mut buf = Cursor::new(Vec::new());
    cropped.write_to(&mut buf, ImageFormat::Png)?;
    std::fs::write(&path, buf.into_inner())?;
    println!("Saved: {}", path.display());

    println!("\nDone! Test with:");
    println!("  EVENT_BUS_ICON={}/icon-512.png terminal-notifier -title Test -message Hi -sender com.apple.Terminal -appIcon {}/icon-512.png",
             assets_dir.display(), assets_dir.display());

    Ok(())
}
