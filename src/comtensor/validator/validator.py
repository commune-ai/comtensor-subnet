"""
CommuneX example of a Text Validator Module

This module provides an example TextValidator class for validating text generated by modules in subnets.
The TextValidator retrieves module addresses from the subnet, prompts the modules to generate answers to a given question,
and scores the generated answers against the validator's own answers.

Classes:
    TextValidator: A class for validating text generated by modules in a subnet.

Functions:
    set_weights: Blockchain call to set weights for miners based on their scores.
    cut_to_max_allowed_weights: Cut the scores to the maximum allowed weights.
    extract_address: Extract an address from a string.
    get_subnet_netuid: Retrieve the network UID of the subnet.
    get_ip_port: Get the IP and port information from module addresses.

Constants:
    IP_REGEX: A regular expression pattern for matching IP addresses.
"""

import asyncio
import concurrent.futures
import re
import time
from functools import partial
from datatrove.pipeline.readers import ParquetReader
import random

from communex.client import CommuneClient  # type: ignore
from communex.module.client import ModuleClient  # type: ignore
from communex.module.module import Module  # type: ignore
from communex.types import Ss58Address  # type: ignore
from substrateinterface import Keypair  # type: ignore

from sklearn.feature_extraction.text import CountVectorizer
from scipy.spatial.distance import dice
from difflib import SequenceMatcher
from jellyfish import jaro_winkler_similarity

from ._config import ValidatorSettings
from ..utils import log

IP_REGEX = re.compile(r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}:\d+")


def set_weights(
    settings: ValidatorSettings,
    score_dict: dict[
        int, float
    ],  # implemented as a float score from 0 to 1, one being the best
    # you can implement your custom logic for scoring
    netuid: int,
    client: CommuneClient,
    key: Keypair,
) -> None:
    """
    Set weights for miners based on their scores.

    Args:
        score_dict: A dictionary mapping miner UIDs to their scores.
        netuid: The network UID.
        client: The CommuneX client.
        key: The keypair for signing transactions.
    """

    # you can replace with `max_allowed_weights` with the amount your subnet allows
    score_dict = cut_to_max_allowed_weights(score_dict, settings.max_allowed_weights)

    # Create a new dictionary to store the weighted scores
    weighted_scores: dict[int, int] = {}

    # Calculate the sum of all inverted scores
    scores = sum(score_dict.values())

    # process the scores into weights of type dict[int, int] 
    # Iterate over the items in the score_dict
    for uid, score in score_dict.items():
        # Calculate the normalized weight as an integer
        weight = int(score * 1000 / scores)

        # Add the weighted score to the new dictionary
        weighted_scores[uid] = weight


    # filter out 0 weights
    weighted_scores = {k: v for k, v in weighted_scores.items() if v != 0}

    uids = list(weighted_scores.keys())
    weights = list(weighted_scores.values())
    # send the blockchain call
    client.vote(key=key, uids=uids, weights=weights, netuid=netuid)


def cut_to_max_allowed_weights(
    score_dict: dict[int, float], max_allowed_weights: int
) -> dict[int, float]:
    """
    Cut the scores to the maximum allowed weights.

    Args:
        score_dict: A dictionary mapping miner UIDs to their scores.
        max_allowed_weights: The maximum allowed weights (default: 420).

    Returns:
        A dictionary mapping miner UIDs to their scores, where the scores have been cut to the maximum allowed weights.
    """
    # sort the score by highest to lowest
    sorted_scores = sorted(score_dict.items(), key=lambda x: x[1], reverse=True)

    # cut to max_allowed_weights
    cut_scores = sorted_scores[:max_allowed_weights]

    return dict(cut_scores)


def extract_address(string: str):
    """
    Extracts an address from a string.
    """
    return re.search(IP_REGEX, string)


def get_subnet_netuid(clinet: CommuneClient, subnet_name: str = "replace-with-your-subnet-name"):
    """
    Retrieve the network UID of the subnet.

    Args:
        client: The CommuneX client.
        subnet_name: The name of the subnet (default: "foo").

    Returns:
        The network UID of the subnet.

    Raises:
        ValueError: If the subnet is not found.
    """

    subnets = clinet.query_map_subnet_names()
    for netuid, name in subnets.items():
        if name == subnet_name:
            return netuid
    raise ValueError(f"Subnet {subnet_name} not found")


def get_ip_port(modules_adresses: dict[int, str]):
    """
    Get the IP and port information from module addresses.

    Args:
        modules_addresses: A dictionary mapping module IDs to their addresses.

    Returns:
        A dictionary mapping module IDs to their IP and port information.
    """

    filtered_addr = {id: extract_address(addr) for id, addr in modules_adresses.items()}
    ip_port = {
        id: x.group(0).split(":") for id, x in filtered_addr.items() if x is not None
    }
    return ip_port


class TextValidator(Module):
    """
    A class for validating text generated by modules in a subnet.

    Attributes:
        client: The CommuneClient instance used to interact with the subnet.
        key: The keypair used for authentication.
        netuid: The unique identifier of the subnet.
        val_model: The validation model used for scoring answers.
        call_timeout: The timeout value for module calls in seconds (default: 60).

    Methods:
        get_modules: Retrieve all module addresses from the subnet.
        _get_miner_prediction: Prompt a miner module to generate an answer to the given question.
        _score_miner: Score the generated answer against the validator's own answer.
        get_miner_prompt: Generate a prompt for the miner modules.
        validate_step: Perform a validation step by generating questions, prompting modules, and scoring answers.
        validation_loop: Run the validation loop continuously based on the provided settings.
    """

    def __init__(
        self,
        key: Keypair,
        netuid: int,
        client: CommuneClient,
        call_timeout: int = 60,
    ) -> None:
        super().__init__()
        self.client = client
        self.key = key
        self.netuid = netuid
        self.val_model = "foo"
        self.call_timeout = call_timeout

    def get_addresses(self, client: CommuneClient, netuid: int) -> dict[int, str]:
        """
        Retrieve all module addresses from the subnet.

        Args:
            client: The CommuneClient instance used to query the subnet.
            netuid: The unique identifier of the subnet.

        Returns:
            A dictionary mapping module IDs to their addresses.
        """

        # Makes a blockchain query for the miner addresses
        module_addreses = client.query_map_address(netuid)
        return module_addreses

    def _get_miner_prediction(
        self,
        question: str,
        miner_info: tuple[list[str], Ss58Address],
    ) -> str | None:
        """
        Prompt a miner module to generate an answer to the given question.

        Args:
            question: The question to ask the miner module.
            miner_info: A tuple containing the miner's connection information and key.

        Returns:
            The generated answer from the miner module, or None if the miner fails to generate an answer.
        """
        connection, miner_key = miner_info
        module_ip, module_port = connection
        client = ModuleClient(module_ip, int(module_port), self.key)
        uid = self.client.get_uids(miner_key, self.netuid)
        log(f"--->⛏️ UID:{uid} {miner_info}")
        try:
            # handles the communication with the miner
            start_time = time.time()
            miner_answer = asyncio.run(
                client.call(
                    "generate",
                    miner_key,
                    {
                        "prompt": question,
                        "type": "prompt",
                        "netuid": 18
                     },
                    timeout=self.call_timeout,  #  type: ignore
                )
            )
            miner_answer = miner_answer["answer"]
            end_time = time.time()
            response_time = end_time - start_time
            miner_answer["response_time"] = response_time
            log(f"<---✅ UID:{uid} {miner_info}")

        except Exception as e:
            log(f"<---❌ Miner UID:{uid} {module_ip}:{module_port} failed to generate an answer")
            print(e)
            miner_answer = None
        return miner_answer

    def calculate_jaro_winkler_scores(self, strings):
        n = len(strings)
        scores = [0] * n
        for i in range(n):
            for j in range(n):
                if i != j:
                    similarity = jaro_winkler_similarity(strings[i], strings[j])
                    scores[i] += similarity * 0.1
        return scores

    def calculate_dice_similarity_scores(self, strings):
        vectorizer = CountVectorizer(analyzer='char', ngram_range=(2, 2))
        X = vectorizer.fit_transform(strings).toarray()
        n = len(strings)
        scores = [0] * n
        for i in range(n):
            for j in range(n):
                if i != j:
                    similarity = 1 - dice(X[i], X[j])
                    scores[i] += similarity * 0.1
        return scores

    def calculate_ratcliff_obershelp_scores(self, strings):
        n = len(strings)
        scores = [0] * n
        for i in range(n):
            for j in range(n):
                if i != j:
                    similarity = SequenceMatcher(None, strings[i], strings[j]).ratio()
                    scores[i] += similarity * 0.1
        return scores


    def _score_miner(self, miner_res, miner_uids, miner_time):
        """
        Score the generated answer against the validator's own answer.

        Args:
            miner_answer: The generated answer from the miner module.

        Returns:
            The score assigned to the miner's answer.
        """

        jaro_winkler_scores = self.calculate_jaro_winkler_scores(miner_res)
        dice_scores = self.calculate_dice_similarity_scores(miner_res)
        ratcliff_obershelp_scores = self.calculate_ratcliff_obershelp_scores(miner_res)

        score_dict: dict[int, float] = {}
        for i, time in enumerate(miner_time):
            time = max(time, 1)
            score_dict[miner_uids[i]] = (jaro_winkler_scores[i] + dice_scores[i] + ratcliff_obershelp_scores[i]) / 3 * 0.8 + (1 / time) * 0.2
        return score_dict

    def get_miner_prompt(self) -> str:
        """
        Generate a prompt for the miner modules.

        Returns:
            The generated prompt for the miner modules.
        """

        # Implement your custom prompt generation logic here
        data_reader = ParquetReader("hf://datasets/HuggingFaceFW/fineweb/data", limit=100) 
        random_number = random.randint(0, 99)
        for i, document in enumerate(data_reader()):
            if random_number < i:
                return document.text

    async def validate_step(
        self, syntia_netuid: int, settings: ValidatorSettings
    ) -> None:
        """
        Perform a validation step.

        Generates questions based on the provided settings, prompts modules to generate answers,
        and scores the generated answers against the validator's own answers.

        Args:
            syntia_netuid: The network UID of the subnet.
        """

        # retrive the miner information
        modules_adresses = self.get_addresses(self.client, syntia_netuid)
        modules_keys = self.client.query_map_key(syntia_netuid)
        val_ss58 = self.key.ss58_address
        if val_ss58 not in modules_keys.values():
            raise RuntimeError(f"validator key {val_ss58} is not registered in subnet")

        modules_info: dict[int, tuple[list[str], Ss58Address]] = {}

        modules_filtered_address = get_ip_port(modules_adresses)
        for module_id in modules_keys.keys():
            module_addr = modules_filtered_address.get(module_id, None)
            if not module_addr:
                continue
            modules_info[module_id] = (module_addr, modules_keys[module_id])

        score_dict: dict[int, float] = {}

        miner_prompt = self.get_miner_prompt()
        get_miner_prediction = partial(self._get_miner_prediction, miner_prompt)

        log(f"Selected the following miners: {modules_info.keys()}")

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            it = executor.map(get_miner_prediction, modules_info.values())
            miner_answers = [*it]

        miner_res, miner_time, miner_uids = [], [], []
        for uid, miner_response in zip(modules_info.keys(), miner_answers):
            miner_answer = miner_response
            if not miner_answer:
                log(f"Skipping miner {uid} that didn't answer")
                continue

            miner_res.append(miner_response['response'])
            miner_time.append(miner_response['response_time'])
            miner_uids.append(uid)
        
        score_dict = self._score_miner(miner_res, miner_uids, miner_time)

        if not score_dict:
            log("No miner managed to give a valid answer")
            return None

        # the blockchain call to set the weights
        _ = set_weights(settings, score_dict, self.netuid, self.client, self.key)
        log(f"✅ Set weights successfully {score_dict}")

    def validation_loop(self, settings: ValidatorSettings) -> None:
        """
        Run the validation loop continuously based on the provided settings.

        Args:
            settings: The validator settings to use for the validation loop.
        """

        while True:
            start_time = time.time()
            _ = asyncio.run(self.validate_step(self.netuid, settings))

            elapsed = time.time() - start_time
            if elapsed < settings.iteration_interval:
                sleep_time = settings.iteration_interval - elapsed
                log(f"Sleeping for {sleep_time}")
                time.sleep(sleep_time)
