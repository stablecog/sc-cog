import time
import os

import torch
from diffusers import (
    StableDiffusionPipeline,
)
from cog import BasePredictor, Input, Path

from models.stable_diffusion.generate import generate
from models.stable_diffusion.constants import (
    SD_MODEL_CHOICES,
    SD_MODELS,
    SD_MODEL_CACHE,
    SD_MODEL_DEFAULT,
    SD_SCHEDULER_DEFAULT,
    SD_SCHEDULER_CHOICES,
    SD_MODEL_DEFAULT_KEY,
    SD_MODEL_DEFAULT_ID,
)
from models.stable_diffusion.helpers import download_sd_models_concurrently
from models.nllb.translate import translate_text
from models.swinir.upscale import upscale

from lingua import LanguageDetectorBuilder
import cv2

version = "0.1.72"


class Predictor(BasePredictor):
    def setup(self):
        print(f"⏳ Setup has started - Version: {version}")

        if os.environ.get("DOWNLOAD_MODELS_ON_SETUP", "1") == "1":
            download_sd_models_concurrently()

        print(f"⏳ Loading the default pipeline: {SD_MODEL_DEFAULT_ID}")
        self.txt2img = StableDiffusionPipeline.from_pretrained(
            SD_MODEL_DEFAULT["id"],
            torch_dtype=SD_MODEL_DEFAULT["torch_dtype"],
            cache_dir=SD_MODEL_CACHE,
        )
        self.txt2img_pipe = self.txt2img.to("cuda")
        self.txt2img_pipe.enable_xformers_memory_efficient_attention()
        print(f"✅ Loaded the default pipeline: {SD_MODEL_DEFAULT_ID}")

        self.txt2img_alts = {}
        self.txt2img_alt_pipes = {}
        for key in SD_MODELS:
            if key != SD_MODEL_DEFAULT_KEY:
                print(f"⏳ Loading model: {key}")
                self.txt2img_alts[key] = StableDiffusionPipeline.from_pretrained(
                    SD_MODELS[key]["id"],
                    torch_dtype=SD_MODELS[key]["torch_dtype"],
                    cache_dir=SD_MODEL_CACHE,
                )
                self.txt2img_alt_pipes[key] = self.txt2img_alts[key].to("cuda")
                self.txt2img_alt_pipes[key].enable_xformers_memory_efficient_attention()
                print(f"✅ Loaded model: {key}")

        # For translation
        self.detect_language = (
            LanguageDetectorBuilder.from_all_languages()
            .with_preloaded_language_models()
            .build()
        )
        print("✅ Loaded language detector")

        print("✅ Setup is done!")

    @torch.inference_mode()
    def predict(
        self,
        prompt: str = Input(description="Input prompt.", default=""),
        negative_prompt: str = Input(description="Input negative prompt.", default=""),
        width: int = Input(
            description="Width of output image.",
            choices=[128, 256, 384, 448, 512, 576, 640, 704, 768, 832, 896, 960, 1024],
            default=512,
        ),
        height: int = Input(
            description="Height of output image.",
            choices=[128, 256, 384, 448, 512, 576, 640, 704, 768, 832, 896, 960, 1024],
            default=512,
        ),
        num_outputs: int = Input(
            description="Number of images to output. If the NSFW filter is triggered, you may get fewer outputs than this.",
            ge=1,
            le=10,
            default=1,
        ),
        num_inference_steps: int = Input(
            description="Number of denoising steps", ge=1, le=500, default=30
        ),
        guidance_scale: float = Input(
            description="Scale for classifier-free guidance", ge=1, le=20, default=7.5
        ),
        scheduler: str = Input(
            default=SD_SCHEDULER_DEFAULT,
            choices=SD_SCHEDULER_CHOICES,
            description="Choose a scheduler.",
        ),
        model: str = Input(
            default=SD_MODEL_DEFAULT_KEY,
            choices=SD_MODEL_CHOICES,
            description="Choose a model. Defaults to 'Stable Diffusion v1.5'.",
        ),
        seed: int = Input(
            description="Random seed. Leave blank to randomize the seed.", default=None
        ),
        prompt_flores_200_code: str = Input(
            description="Prompt language code (FLORES-200). It overrides the language auto-detection.",
            default=None,
        ),
        negative_prompt_flores_200_code: str = Input(
            description="Negative prompt language code (FLORES-200). It overrides the language auto-detection.",
            default=None,
        ),
        prompt_prefix: str = Input(description="Prompt prefix.", default=None),
        negative_prompt_prefix: str = Input(
            description="Negative prompt prefix.", default=None
        ),
        output_image_extension: str = Input(
            description="Output type of the image. Can be 'png' or 'jpeg' or 'webp'.",
            choices=["png", "jpeg", "webp"],
            default="jpeg",
        ),
        output_image_quality: int = Input(
            description="Output quality of the image. Can be 1-100.", default=90
        ),
        image_to_upscale: Path = Input(
            description="Input image for the upscaler (Swinir).", default=None
        ),
        process_type: str = Input(
            description="Choose a process type. Can be 'generate', 'upscale' or 'generate_and_upscale'. Defaults to 'generate'",
            choices=["generate", "upscale", "generate_and_upscale"],
            default="generate",
        ),
        translator_cog_url: str = Input(
            description="URL of the translator cog. If it's blank, TRANSLATOR_COG_URL environment variable will be used (if it exists).",
            default=None,
        ),
    ) -> dict[str, list[Path] | int]:
        processStart = time.time()
        print("//////////////////////////////////////////////////////////////////")
        print(f"⏳ Process started: {process_type} ⏳")
        output_images = []
        output_paths = []
        nsfw_count = 0

        if process_type == "generate" or process_type == "generate_and_upscale":
            if translator_cog_url is None:
                translator_cog_url = os.environ.get("TRANSLATOR_COG_URL", None)

            t_prompt = prompt
            t_negative_prompt = negative_prompt
            if translator_cog_url is not None:
                [t_prompt, t_negative_prompt] = translate_text(
                    prompt,
                    prompt_flores_200_code,
                    negative_prompt,
                    negative_prompt_flores_200_code,
                    translator_cog_url,
                    self.detect_language,
                    "Prompt & Negative Prompt",
                )
            else:
                print("-- Translator cog URL is not set. Skipping translation. --")

            txt2img_pipe = None
            if model != SD_MODEL_DEFAULT_KEY:
                txt2img_pipe = self.txt2img_alt_pipes[model]
            else:
                txt2img_pipe = self.txt2img_pipe

            print(
                f"🖥️ Generating - Model: {model} - Width: {width} - Height: {height} - Steps: {num_inference_steps} - Outputs: {num_outputs} 🖥️"
            )
            startTime = time.time()
            generate_output_images, generate_nsfw_count = generate(
                t_prompt,
                t_negative_prompt,
                prompt_prefix,
                negative_prompt_prefix,
                width,
                height,
                num_outputs,
                num_inference_steps,
                guidance_scale,
                scheduler,
                seed,
                model,
                txt2img_pipe,
            )
            output_images = generate_output_images
            nsfw_count = generate_nsfw_count
            endTime = time.time()
            print(
                f"🖥️ Generated in {round((endTime - startTime) * 1000)} ms - Model: {model} - Width: {width} - Height: {height} - Steps: {num_inference_steps} - Outputs: {num_outputs} 🖥️"
            )

        if process_type == "upscale" or process_type == "generate_and_upscale":
            startTime = time.time()
            if process_type == "upscale":
                upscale_output_path = upscale(image_to_upscale)
                output_paths = [upscale_output_path]
            else:
                upscale_output_paths = []
                for image in output_images:
                    upscale_output_path = upscale(image)
                    upscale_output_paths.append(upscale_output_path)
                output_paths = upscale_output_paths
            endTime = time.time()
            print(f"-- Upscaled in: {round((endTime - startTime) * 1000)} ms --")

        # Convert to output images to the desired format
        conversion_start = time.time()
        if output_image_extension == "png":
            print(f"-- Writing png to the file system --")
            for i, image in enumerate(output_images):
                output_path_converted = f"/tmp/out-{i}.png"
                cv2.imwrite(output_path_converted, image)
                output_paths[i] = Path(output_path_converted)
        else:
            print(
                f"-- Converting - {output_image_extension} - {output_image_quality} --"
            )
            quality_type = cv2.IMWRITE_JPEG_QUALITY
            if output_image_extension == "webp":
                quality_type = cv2.IMWRITE_WEBP_QUALITY
            for i, image in enumerate(output_images):
                output_path_converted = f"/tmp/out-{i}.{output_image_extension}"
                cv2.imwrite(
                    output_path_converted,
                    image,
                    [int(quality_type), output_image_quality],
                )
                output_paths[i] = Path(output_path_converted)
        conversion_end = time.time()
        if output_image_extension == "png":
            print(
                f"-- Wrote png to the file system in: {round((conversion_end - conversion_start) *1000)} ms --"
            )
        else:
            print(
                f"-- Converted in: {round((conversion_end - conversion_start) *1000)} ms - {output_image_extension} - {output_image_quality} --"
            )

        processEnd = time.time()
        print(
            f"✅ Process completed in: {round((processEnd - processStart) * 1000)} ms ✅"
        )
        print("//////////////////////////////////////////////////////////////////")
        return {
            "outputs": output_paths,
            "nsfw_count": nsfw_count,
        }
