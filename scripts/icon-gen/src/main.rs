//! Generate event-bus notification icon using Gemini image generation.
//!
//! Creates a pixel art Birman cat with ball of yarn for macOS notifications.
//!
//! # Usage
//!
//! ```bash
//! GEMINI_API_KEY=your_key cargo run --release
//! # or with custom prompt:
//! GEMINI_API_KEY=your_key cargo run --release -- "your custom prompt"
//! ```

use image::imageops::FilterType;
use image::ImageFormat;
use rust_genai::{Client, InteractionResponseExt, InteractionStatus};
use std::env;
use std::io::Cursor;
use std::path::PathBuf;

const DEFAULT_PROMPT: &str = r#"Create a pixel art style icon (32x32 pixels scaled up) of a cute white and orange Birman cat with bright blue eyes, playfully batting at a colorful ball of yarn. The yarn ball should have rainbow colors (red, orange, yellow, green, blue, purple). The cat should have the characteristic Birman coloring: creamy white body with orange/seal points on the face, ears, and paws. The style should be clean pixel art suitable for a macOS notification icon. Transparent background. The cat should look happy and playful."#;

fn save_image(bytes: &[u8], output_dir: &PathBuf) -> Result<(), Box<dyn std::error::Error>> {
    // Load image from bytes (already decoded)
    let img = image::load_from_memory(bytes)?;
    println!("Original size: {}x{}", img.width(), img.height());

    // Crop to square (center crop)
    let (w, h) = (img.width(), img.height());
    let size = w.min(h);
    let left = (w - size) / 2;
    let top = (h - size) / 2;
    let img_square = img.crop_imm(left, top, size, size);

    // Create output directory
    std::fs::create_dir_all(output_dir)?;

    // Save at different sizes
    for target_size in [512u32, 1024u32] {
        let resized = img_square.resize(target_size, target_size, FilterType::Lanczos3);
        let path = output_dir.join(format!("icon-{}.png", target_size));

        let mut buf = Cursor::new(Vec::new());
        resized.write_to(&mut buf, ImageFormat::Png)?;
        std::fs::write(&path, buf.into_inner())?;

        println!("Saved: {}", path.display());
    }

    // Save original cropped version
    let path = output_dir.join("icon.png");
    let mut buf = Cursor::new(Vec::new());
    img_square.write_to(&mut buf, ImageFormat::Png)?;
    std::fs::write(&path, buf.into_inner())?;
    println!("Saved: {}", path.display());

    Ok(())
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let api_key = env::var("GEMINI_API_KEY").expect("GEMINI_API_KEY environment variable not set");

    let client = Client::builder(api_key).build();

    // Get prompt from args or use default
    let args: Vec<String> = env::args().collect();
    let prompt = if args.len() > 1 {
        args[1..].join(" ")
    } else {
        DEFAULT_PROMPT.to_string()
    };

    println!("=== EVENT BUS ICON GENERATION ===\n");
    println!("Prompt: {}\n", &prompt[..100.min(prompt.len())]);

    let model = "gemini-3-pro-image-preview";

    let result = client
        .interaction()
        .with_model(model)
        .with_text(&prompt)
        .with_image_output()
        .create()
        .await;

    match result {
        Ok(response) => {
            println!("Status: {:?}", response.status);

            if response.status == InteractionStatus::Completed {
                // Get assets directory (relative to this crate)
                let manifest_dir = env!("CARGO_MANIFEST_DIR");
                let output_dir = PathBuf::from(manifest_dir)
                    .parent()
                    .unwrap()
                    .parent()
                    .unwrap()
                    .join("assets");

                // Use new DX helper - no manual base64 decoding needed!
                let bytes = response
                    .first_image_bytes()?
                    .ok_or("No image in response")?;

                save_image(&bytes, &output_dir)?;
                println!("\nIcons saved to: {}", output_dir.display());
                println!(
                    "\nTo use:\n  EVENT_BUS_ICON={}/icon-512.png event-bus",
                    output_dir.display()
                );
            }

            if let Some(usage) = &response.usage {
                println!(
                    "\nTokens: {} in / {} out",
                    usage.total_input_tokens.unwrap_or(0),
                    usage.total_output_tokens.unwrap_or(0)
                );
            }
        }
        Err(e) => {
            eprintln!("Error: {}", e);
            return Err(e.into());
        }
    }

    Ok(())
}
