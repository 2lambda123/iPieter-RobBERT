from flask import Flask, request
import os
from transformers import (
    RobertaForSequenceClassification,
    RobertaForMaskedLM,
    RobertaTokenizer,
)
import torch
import nltk
from nltk.tokenize.treebank import TreebankWordDetokenizer
import json
import re


def replace_query_token(sentence):
    "Small utility function to replace a sentence with `_die_` or `_dat_` with the proper RobBERT input."
    tokens = nltk.word_tokenize(sentence)
    tokens_swapped = nltk.word_tokenize(sentence)
    for i, word in enumerate(tokens):
        if word == "_die_":
            tokens[i] = "die"
            tokens_swapped[i] = "dat"

        elif word == "_dat_":
            tokens[i] = "dat"
            tokens_swapped[i] = "die"

        elif word == "_Dat_":
            tokens[i] = "Dat"
            tokens_swapped[i] = "Die"

        elif word == "_Die_":
            tokens[i] = "Die"

            tokens_swapped[i] = "Dat"

        if word.lower() == "_die_" or word.lower() == "_dat_":
            results = TreebankWordDetokenizer().detokenize(tokens)
            results_swapped = TreebankWordDetokenizer().detokenize(tokens_swapped)

            return "{} <sep> {}".format(results, results_swapped)

    # If we reach the end of the for loop, it means no query token was present
    raise ValueError("'die' or 'dat' should be surrounded by underscores.")


def create_app(model_path: str, fast_model_path: str, device="cpu"):
    """
    Create the flask app.

    :param model_path: Path to the finetuned model.
    :param device: Pytorch device, default CPU (switch to 'cuda' if a GPU is present)
    :return: the flask app
    """
    app = Flask(__name__, instance_relative_config=True)

    print("initializing tokenizer and RobBERT.")
    if model_path:
        tokenizer: RobertaTokenizer = RobertaTokenizer.from_pretrained(
            model_path, use_auth_token=True
        )
        robbert = RobertaForSequenceClassification.from_pretrained(
            model_path, use_auth_token=True
        )
        robbert.eval()
        print("Loaded finetuned model")

    if fast_model_path:
        fast_tokenizer: RobertaTokenizer = RobertaTokenizer.from_pretrained(
            fast_model_path, use_auth_token=True
        )
        fast_robbert = RobertaForMaskedLM.from_pretrained(
            fast_model_path, use_auth_token=True
        )
        fast_robbert.eval()

        print("Loaded MLM model")

        possible_tokens = ["die", "dat", "Die", "Dat"]

        ids = fast_tokenizer.convert_tokens_to_ids(possible_tokens)

    mask_padding_with_zero = True
    block_size = 512

    # Disable dropout

    nltk.download("punkt")

    if fast_model_path:

        @app.route("/disambiguation/mlm/all", methods=["POST"])
        def split():
            sentence = request.form["sentence"]

            response = []
            old_pos = 0
            for match in re.finditer(r"(die|dat|Die|Dat)+", sentence):
                print(
                    "match",
                    match.group(),
                    "start index",
                    match.start(),
                    "End index",
                    match.end(),
                )
                with torch.no_grad():
                    query = (
                        sentence[: match.start()] + "<mask>" + sentence[match.end() :]
                    )
                    print(query)
                    if match.start() > 0:
                        response.append({"part": sentence[old_pos: match.start()]})

                    old_pos = match.end()
                    inputs = fast_tokenizer.encode_plus(query, return_tensors="pt")

                    outputs = fast_robbert(**inputs)
                    masked_position = torch.where(
                        inputs["input_ids"] == fast_tokenizer.mask_token_id
                    )[1]
                    if len(masked_position) > 1:
                        return "No two queries allowed in one sentence.", 400

                    print(outputs.logits[0, masked_position, ids])
                    token = outputs.logits[0, masked_position, ids].argmax()

                    confidence = float(outputs.logits[0, masked_position, ids].max())

                    response.append({
                        "predicted": possible_tokens[token],
                        "input": match.group(),
                        "interpretation": "correct"
                        if possible_tokens[token] == match.group()
                        else "incorrect",
                        "confidence": confidence,
                        "sentence": sentence,
                    })

                    # This would be a good place for logging/storing queries + results
                    print(response)

            # inputs = fast_tokenizer.encode_plus(query, return_tensors="pt")
            response.append({"part": sentence[match.end():]})
            return json.dumps(response)

        @app.route("/disambiguation/mlm", methods=["POST"])
        def fast():
            sentence = request.form["sentence"]
            for i, x in enumerate(possible_tokens):
                if f"_{x}_" in sentence:
                    masked_id = i
                    query = sentence.replace(f"_{x}_", fast_tokenizer.mask_token)

            inputs = fast_tokenizer.encode_plus(query, return_tensors="pt")

            masked_position = torch.where(
                inputs["input_ids"] == fast_tokenizer.mask_token_id
            )[1]
            if len(masked_position) > 1:
                return "No two queries allowed in one sentence.", 400

            # self.examples.append([tokenizer.build_inputs_with_special_tokens(tokenized_text[0 : block_size]), [0], [0]])
            with torch.no_grad():
                outputs = fast_robbert(**inputs)

                print(outputs.logits[0, masked_position, ids])
                token = outputs.logits[0, masked_position, ids].argmax()

                confidence = float(outputs.logits[0, masked_position, ids].max())

                response = {
                    "rating": possible_tokens[token],
                    "interpretation": "correct" if token == masked_id else "incorrect",
                    "confidence": confidence,
                    "sentence": sentence,
                }

                # This would be a good place for logging/storing queries + results
                print(response)

                return json.dumps(response)

    if model_path:

        @app.route("/disambiguation/classifier", methods=["POST"])
        def main():
            sentence = request.form["sentence"]
            query = replace_query_token(sentence)

            tokenized_text = tokenizer.encode(
                tokenizer.tokenize(query)[-block_size + 3 : -1]
            )

            input_mask = [1 if mask_padding_with_zero else 0] * len(tokenized_text)

            pad_token = tokenizer.convert_tokens_to_ids(tokenizer.pad_token)
            while len(tokenized_text) < block_size:
                tokenized_text.append(pad_token)
                input_mask.append(0 if mask_padding_with_zero else 1)
                # segment_ids.append(pad_token_segment_id)
                # p_mask.append(1)

            # self.examples.append([tokenizer.build_inputs_with_special_tokens(tokenized_text[0 : block_size]), [0], [0]])
            batch = tuple(
                torch.tensor(t).to(torch.device(device))
                for t in [
                    tokenized_text[0 : block_size - 3],
                    input_mask[0 : block_size - 3],
                    [0],
                    [1][0],
                ]
            )
            inputs = {
                "input_ids": batch[0].unsqueeze(0),
                "attention_mask": batch[1].unsqueeze(0),
                "labels": batch[3].unsqueeze(0),
            }
            with torch.no_grad():
                outputs = robbert(**inputs)

                rating = outputs[1].argmax().item()
                confidence = outputs[1][0, rating].item()

                response = {
                    "rating": rating,
                    "interpretation": "incorrect" if rating == 1 else "correct",
                    "confidence": confidence,
                    "sentence": sentence,
                }

                # This would be a good place for logging/storing queries + results
                print(response)

                return (json.dumps(response), {'Content-Type': 'application/json'})

    return app
