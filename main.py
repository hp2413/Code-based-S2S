import os
import shutil
import atexit
import threading
import queue
from typing import Callable, Iterator, Optional
from fastapi import WebSocket
import numpy as np
import time
from asr.asr_factory import ASRFactory
from asr.asr_interface import ASRInterface
from live2d_model import Live2dModel
from llm.llm_factory import LLMFactory
from llm.llm_interface import LLMInterface
from prompts import prompt_loader
from tts.tts_factory import TTSFactory
from tts.tts_interface import TTSInterface

import yaml
import random
import ollama
import asyncio
from langchain_community.vectorstores import Chroma
from langchain_community.embeddings import OllamaEmbeddings
from langchain_community.document_loaders import DirectoryLoader
from langchain.text_splitter import RecursiveCharacterTextSplitter
from langchain.schema import Document 

class OpenLLMVTuberMain:
    """
    The main class for the OpenLLM VTuber.
    It initializes the Live2D controller, ASR, TTS, and LLM based on the provided configuration.
    Run `conversation_chain` to start one conversation (user_input -> llm -> speak).

    Attributes:
    - config (dict): The configuration dictionary.
    - llm (LLMInterface): The LLM instance.
    - asr (ASRInterface): The ASR instance.
    - tts (TTSInterface): The TTS instance.
    """

    config: dict
    llm: LLMInterface
    asr: ASRInterface
    tts: TTSInterface
    live2d: Live2dModel | None
    _continue_exec_flag: threading.Event
    EXEC_FLAG_CHECK_TIMEOUT = 5  # seconds

    def __init__(
        self,
        configs: dict,
        custom_asr: ASRInterface | None = None,
        custom_tts: TTSInterface | None = None,
        websocket: WebSocket | None = None,
    ) -> None:
        self.config = configs
        self.verbose = self.config.get("VERBOSE", False)
        self.show_timing = self.config.get("SHOW_RESPONSE_TIME", False)
        self.websocket = websocket
        # self.live2d = self.init_live2d()
        self._continue_exec_flag = threading.Event()
        self._continue_exec_flag.set()  # Set the flag to continue execution
        
        print(f"is show response time enabled? {self.show_timing}")
        # Init RAG and load the docs.
        if self.config.get("RAG_ON", False):
            print("RAG is enabled")
            self.retriever = self.init_vectorstore()
        else:
            print("RAG is disabled.")
            self.retriever = None

        # Init ASR if voice input is on.
        if self.config.get("VOICE_INPUT_ON", False):
            # if custom_asr is provided, don't init asr and use it instead.
            if custom_asr is None:
                self.asr = self.init_asr()
            else:
                print("Using custom ASR")
                self.asr = custom_asr
        else:
            self.asr = None

        # Init TTS if TTS is on.
        if self.config.get("TTS_ON", False):
            # if custom_tts is provided, don't init tts and use it instead.
            if custom_tts is None:
                self.tts = self.init_tts()
            else:
                print("Using custom TTS")
                self.tts = custom_tts
        else:
            self.tts = None

        self.llm = self.init_llm()

        self._play_audio_file(
                        sentence="Welcome note",
                        filepath=f"./Audio_Files/Welcome_audio.mp3",
                        remove_after_play = False
                    )
        
    # Initialization methods

    # def init_live2d(self) -> Live2dModel | None:
    #     if not self.config.get("LIVE2D", False):
    #         return None
    #     try:
    #         live2d_model_name = self.config.get("LIVE2D_MODEL")
    #         live2d_controller = Live2dModel(live2d_model_name)
    #     except Exception as e:
    #         print(f"Error initializing Live2D: {e}")
    #         print("Proceed without Live2D.")
    #         return None
    #     return live2d_controller

    # Initialization rag
    def init_vectorstore(self) -> Chroma:
        print("Innitiate the document loader..")
        # Define the path to the folder where the documents are located
        folder_path = "./data"

        # Specify the models for embeddings
        embedding_model = self.config.get("EMBED_MODEL")
        
        # Load all files from the specified folder
        loader = DirectoryLoader(folder_path)
        print("Loading vault content in vectordb...")

        # Load documents from the directory
        docs = loader.load()

        # Initialize a text splitter to chunk the documents for better processing
        text_splitter = RecursiveCharacterTextSplitter(chunk_size=1000, chunk_overlap=200)

        # Split the loaded documents into smaller chunks
        splits = text_splitter.split_documents(docs)

        # 2. Create Ollama embeddings and vector store
        # Instantiate the embeddings model
        embeddings = OllamaEmbeddings(model=embedding_model)

        # Create a list to hold documents along with their embeddings
        documents_with_embeddings = []
        for doc in splits:
            # Generate an embedding for the current document chunk
            doc_embedding = embeddings.embed_query(doc.page_content)[0]  # Take the first embedding
            # Create a Document object with the embedding stored in its metadata
            documents_with_embeddings.append(
                Document(page_content=doc.page_content, metadata={"embedding": doc_embedding})
            )

        print(f"Loaded {len(documents_with_embeddings)} documents")

        # Add documents with embeddings to the Chroma vector store
        vectorstore = Chroma.from_documents(documents=documents_with_embeddings, embedding=embeddings) 
        return vectorstore.as_retriever()
    
    def init_llm(self) -> LLMInterface:
        llm_provider = self.config.get("LLM_PROVIDER")
        llm_config = self.config.get(llm_provider, {})
        if self.config.get("RAG_ON", False):
            # System message that guides the LLM's responses for RAG
            # system_prompt = self.config.get("RAG_SYSTEM_PROMPT")
            system_prompt = prompt_loader.load_persona(
                self.config.get("RAG_SYSTEM_PROMPT_FILE_NAME")
            )
        else:
            system_prompt = self.get_system_prompt() #old
    
        llm = LLMFactory.create_llm(
            llm_provider=llm_provider, SYSTEM_PROMPT=system_prompt, **llm_config
        )
        return llm

    def init_asr(self) -> ASRInterface:
        asr_model = self.config.get("ASR_MODEL")
        asr_config = self.config.get(asr_model, {})
        if asr_model == "AzureASR":
            import api_keys # type: ignore

            asr_config = {
                "callback": print,
                "subscription_key": api_keys.AZURE_API_Key,
                "region": api_keys.AZURE_REGION,
            }

        asr = ASRFactory.get_asr_system(asr_model, **asr_config)
        return asr

    def init_tts(self) -> TTSInterface:
        tts_model = self.config.get("TTS_MODEL", "pyttsx3TTS")
        tts_config = self.config.get(tts_model, {})

        if tts_model == "AzureTTS":
            import api_keys # type: ignore

            tts_config = {
                "api_key": api_keys.AZURE_API_Key,
                "region": api_keys.AZURE_REGION,
                "voice": api_keys.AZURE_VOICE,
            }
        tts = TTSFactory.get_tts_engine(tts_model, **tts_config)
        return tts

    def set_audio_output_func(
        self, audio_output_func: Callable[[Optional[str], Optional[str]], None]
    ) -> None:
        """
        Set the audio output function to be used for playing audio files.
        The function should accept two arguments: sentence (str) and filepath (str).

        sentence: str | None
        - The sentence to be displayed on the frontend.
        - If None, empty sentence will be displayed.

        filepath: str | None
        - The path to the audio file to be played.
        - If None, no audio will be played.

        Here is an example of the function:
        ~~~python
        def _play_audio_file(sentence: str | None, filepath: str | None) -> None:
            if filepath is None:
                print("No audio to be streamed. Response is empty.")
                return

            if sentence is None:
                sentence = ""
            print(f">> Playing {filepath}...")
            playsound(filepath)
        ~~~
        """

        self._play_audio_file = audio_output_func

        # def _play_audio_file(self, sentence: str, filepath: str | None) -> None:

    def get_system_prompt(self) -> str:
        """
        Construct and return the system prompt based on the configuration file.
        """
        if self.config.get("PERSONA_CHOICE"):
            system_prompt = prompt_loader.load_persona(
                self.config.get("PERSONA_CHOICE")
            )
        else:
            system_prompt = self.config.get("DEFAULT_PERSONA_PROMPT_IN_YAML")

        # if self.live2d is not None:
        #     system_prompt += prompt_loader.load_util(
        #         self.config.get("LIVE2D_Expression_Prompt")
        #     ).replace("[<insert_emomap_keys>]", self.live2d.emo_str)

        if self.verbose:
            print("\n === System Prompt ===")
            print(system_prompt)

        return system_prompt

    
    def combine_docs(self, docs)-> str:
        # Combine the content of retrieved documents into a single string
        return "\n\n".join(doc.page_content for doc in docs)
    
    # TODO: Make this audio play async
    # async def async_play_audio(self, sentence: str, filepath: str, remove_after_play: bool = False):
    #     # Offload the blocking function to a separate thread
    #     print(f"Playing async audio: {filepath}")
    #     await asyncio.to_thread(self._play_audio_file, sentence, filepath, remove_after_play)
    
    # Main conversation methods
    def conversation_chain(self, user_input: str | np.ndarray | None = None) -> str:
        """
        One iteration of the main conversation.
        1. Get user input (text or audio) if not provided as an argument
        2. Call the LLM with the user input
        3. Speak (or not)

        Parameters:
        - user_input (str, numpy array, or None): The user input to be used in the conversation. If it's string, it will be considered as user input. If it's a numpy array, it will be transcribed. If it's None, we'll request input from the user.

        Returns:
        - str: The full response from the LLM
        """

        if not self._continue_exec_flag.wait(
            timeout=self.EXEC_FLAG_CHECK_TIMEOUT
        ):  # Wait for the flag to be set
            print(
                ">> Execution flag not set. In interruption state for too long. Exiting conversation chain."
            )
            raise InterruptedError(
                "Conversation chain interrupted. Wait flag timeout reached."
            )

        # Generate a random number between 0 and 3
        color_code = random.randint(0, 3)
        c = [None] * 4
        # Define the color codes for red, blue, green, and white
        c[0] = "\033[91m"
        c[1] = "\033[94m"
        c[2] = "\033[92m"
        c[3] = "\033[0m"

        # Apply the color to the console output
        print(f"{c[color_code]}New Conversation Chain started!")

        # if user_input is not string, make it string
        if user_input is None:
            user_input = self.get_user_input()
        elif isinstance(user_input, np.ndarray):
            print("transcribing...")
            user_input = self.asr.transcribe_np(user_input)

        if user_input.strip().lower() == self.config.get("EXIT_PHRASE", "exit").lower():
            print("Exiting...")
            exit()

        print(f"User input: {user_input}")

        # only express waiting sound when the rag is enabled
        # Turning off now, untill this audio play async fix
        '''
        if  self.retriever is not None:
            random_number = random.randint(1, 18)
            self._play_audio_file(
                        sentence=user_input,
                        filepath=f"./Audio_Files/temp-{random_number}.mp3",
                        remove_after_play = False
                    )
            #TODO: Make this audio play async
            # print(f"\n -- asyncio start --")
            # play_thread = threading.Thread(target=self._play_audio_file(sentence=user_input,filepath=f"./Audio_Files/temp-{random_number}.mp3", remove_after_play = False))
            # play_thread.start()
            # print(f"\n -- asyncio end --")
        '''

        
        #user_input = "What is the name of the company where I worked as an iOS developer?"
        #user_input = "What is Task Decomposition?"
        #user_input = "Where is the restaurent located?"

        # Start the timer
        if self.show_timing:
            self.process_start_time = time.time()
            print(f"=== Started the vector storage lookup ===")

        if self.config.get("RAG_ON", False):
            # Retrieve relevant documents based on the user's question
            retrieved_docs = self.retriever.invoke(user_input)
            # Combine the contents of the retrieved documents for context
            formatted_context = self.combine_docs(retrieved_docs)
            # Call the LLM with the question and formatted context

            print("Done vector storage lookup")
            # add the context over here
            formatted_prompt = ("Please answer the following question, using the provided context information below, strictly:\n"
                                "---------------------------------------------------------------\n"
                                f"{formatted_context}\n"
                                "---------------------------------------------------------------\n"
                                "If you cannot find a relevant answer based on the context, respond only with **'I don't know the answer!'** "
                                "Do not provide any additional information or reasoning.\n"
                                f"QUESTION: {user_input}\n"
                                "Answer: "
                                ).strip()
        else:
            formatted_prompt = user_input

        print("Starting llm chat")

        # llm call
        chat_completion: Iterator[str] = self.llm.chat_iter(formatted_prompt)

        # End the llm timer
        process_end_llm_time = time.time()

        # Calculate the llm runtime
        if self.show_timing:
            llm_runtime = process_end_llm_time - self.process_start_time
            print(f"\n ---  --- llm runtime: {llm_runtime} seconds  ---  --- ")

        if not self.config.get("TTS_ON", False):
            full_response = ""
            for char in chat_completion:
                if not self._continue_exec_flag.is_set():
                    self._interrupt_post_processing()
                    print("\nInterrupted!")
                    return None
                full_response += char
                print(char, end="")
            return full_response

        full_response = self.speak(chat_completion)

        # End the llm timer
        process_end_tts_time = time.time()

        # Calculate the tts runtime
        if self.show_timing:
            tts_runtime = process_end_tts_time - self.process_start_time
            print(f"\n ---  --- tts total runtime: {tts_runtime} seconds  ---  --- ")

        if self.verbose:
            print(f"\nComplete response: [\n{full_response}\n]")

        print(f"{c[color_code]}Conversation completed.")
        return full_response

    def get_user_input(self) -> str:
        """
        Get user input using the method specified in the configuration file.
        It can be from the console, local microphone, or the browser microphone.

        Returns:
        - str: The user input
        """
        # for live2d with browser, their input are now injected by the server class
        # and they no longer use this method
        if self.config.get("VOICE_INPUT_ON", False):
            # get audio from the local microphone
            print("Listening from the microphone...")
            return self.asr.transcribe_with_local_vad()
        else:
            return input("\n>> ")

    def speak(self, chat_completion: Iterator[str]) -> str:
        """
        Speak the chat completion using the TTS engine.

        Parameters:
        - chat_completion (Iterator[str]): The chat completion to speak

        Returns:
        - str: The full response from the LLM
        """
        full_response = ""
        if self.config.get("SAY_SENTENCE_SEPARATELY", True):
            full_response = self.speak_by_sentence_chain(chat_completion)
        else:  # say the full response at once? how stupid
            full_response = ""
            for char in chat_completion:
                if not self._continue_exec_flag.is_set():
                    print("\nInterrupted!")
                    self._interrupt_post_processing()
                    return None
                print(char, end="")
                full_response += char
            print("\n")
            filename = self._generate_audio_file(full_response, "temp")

            if self._continue_exec_flag.is_set():
                self._play_audio_file(
                    sentence=full_response,
                    filepath=filename,
                )
            else:
                self._interrupt_post_processing()

        return full_response

    def _generate_audio_file(self, sentence: str, file_name_no_ext: str) -> str | None:
        """
        Generate an audio file from the given sentence using the TTS engine.

        Parameters:
        - sentence (str): The sentence to generate audio from
        - file_name_no_ext (str): The name of the audio file without the extension

        Returns:
        - str or None: The path to the generated audio file or None if the sentence is empty
        """
        if self.verbose:
            print(f">> generating {file_name_no_ext}...")

        if not self.tts:
            return None

        # if self.live2d:
        #     sentence = self.live2d.remove_emotion_keywords(sentence)

        if sentence.strip() == "":
            return None

        return self.tts.generate_audio(sentence, file_name_no_ext=file_name_no_ext)

    def _play_audio_file(self, sentence: str | None, filepath: str | None, remove_after_play: bool = True) -> None:
        """
        Play the audio file either locally or remotely using the Live2D controller if available.

        Parameters:
        - sentence (str): The sentence to display
        - filepath (str): The path to the audio file. If None, no audio will be streamed.
        """

        if filepath is None:
            print("No audio to be streamed. Response is empty.")
            return

        if sentence is None:
            sentence = ""

        try:
            if self.verbose:
                print(f">> Playing {filepath}...")

            self.tts.play_audio_file_local(filepath) 

            if remove_after_play:
                self.tts.remove_file(filepath, verbose=self.verbose)
        except ValueError as e:
            if str(e) == "Audio is empty or all zero.":
                print("No audio to be streamed. Response is empty.")
            else:
                raise e
        except Exception as e:
            print(f"Error playing the audio file {filepath}: {e}")

    def speak_by_sentence_chain(self, chat_completion: Iterator[str]) -> str:
        """
        Generate and play the chat completion sentences one by one using the TTS engine.
        Now properly handles interrupts in a multi-threaded environment using the existing _continue_exec_flag.
        """
        task_queue = queue.Queue()
        full_response = [""]  # Use a list to store the full response
        interrupted_error_event = threading.Event()

        def producer_worker():
            try:
                index = 0
                sentence_buffer = ""
                isFirst_Generated = False
                for char in chat_completion:
                    if not self._continue_exec_flag.is_set():
                        raise InterruptedError("Producer interrupted")

                    if char:
                        # print the response on the screen
                        print(char, end="", flush=True)
                        sentence_buffer += char
                        full_response[0] += char
                        if self.is_complete_sentence(char):
                            if self.verbose:
                                print("\n")
                            if not self._continue_exec_flag.is_set():
                                raise InterruptedError("Producer interrupted")
                            audio_filepath = self._generate_audio_file(
                                sentence_buffer, file_name_no_ext=f"temp-{index}"
                            )
                            # Calculate the tts first generation time
                            if self.show_timing and not isFirst_Generated:
                                process_first_audio_genration_time = time.time()
                                tts_first_generation = process_first_audio_genration_time - self.process_start_time
                                print(f"\n ---  --- First audio generation time: {tts_first_generation} seconds  ---  --- ")
                                isFirst_Generated = True
                            if not self._continue_exec_flag.is_set():
                                raise InterruptedError("Producer interrupted")
                            audio_info = {
                                "sentence": sentence_buffer,
                                "audio_filepath": audio_filepath,
                            }
                            task_queue.put(audio_info)
                            index += 1
                            sentence_buffer = ""

                # Handle any remaining text in the buffer
                if sentence_buffer:
                    if not self._continue_exec_flag.is_set():
                        raise InterruptedError("Producer interrupted")
                    print("\n")
                    audio_filepath = self._generate_audio_file(
                        sentence_buffer, file_name_no_ext=f"temp-{index}"
                    )
                    # Calculate the tts first generation time
                    if self.show_timing and not isFirst_Generated:
                        process_first_audio_genration_time = time.time()
                        tts_first_generation = process_first_audio_genration_time - self.process_start_time
                        print(f"\n ---  --- First audio generation time: {tts_first_generation} seconds  at buffer check---  --- ")
                        isFirst_Generated = True
                    audio_info = {
                        "sentence": sentence_buffer,
                        "audio_filepath": audio_filepath,
                    }
                    task_queue.put(audio_info)

            except InterruptedError:
                print("\nProducer interrupted")
                interrupted_error_event.set()
                return  # Exit the function
            except Exception as e:
                print(
                    f"Producer error: Error generating audio for sentence: '{sentence_buffer}'.\n{e}",
                    "Producer stopped\n",
                )
                return
            finally:
                task_queue.put(None)  # Signal end of production

        def consumer_worker():
            heard_sentence = ""
            isFirst_Played = False
            while True:

                try:
                    if not self._continue_exec_flag.is_set():
                        raise InterruptedError("😱Consumer interrupted")

                    audio_info = task_queue.get(
                        timeout=0.1
                    )  # Short timeout to check for interrupts
                    if audio_info is None:
                        break  # End of production
                    if audio_info:
                        # Calculate the tts first play time
                        if self.show_timing and not isFirst_Played:
                            process_first_audio_play_time = time.time()
                            tts_first_play = process_first_audio_play_time - self.process_start_time
                            print(f"\n ---  --- First audio play time: {tts_first_play} seconds  ---  --- ")
                            isFirst_Played = True
                        heard_sentence += audio_info["sentence"]
                        self._play_audio_file(
                            sentence=audio_info["sentence"],
                            filepath=audio_info["audio_filepath"],
                        )
                    task_queue.task_done()
                except queue.Empty:
                    continue  # No item available, continue checking for interrupts
                except InterruptedError as e:
                    print(f"\n{str(e)}, stopping worker threads")
                    interrupted_error_event.set()
                    return  # Exit the function
                except Exception as e:
                    print(
                        f"Consumer error: Error playing sentence '{audio_info['sentence']}'.\n {e}"
                    )
                    continue

        producer_thread = threading.Thread(target=producer_worker)
        consumer_thread = threading.Thread(target=consumer_worker)

        producer_thread.start()
        consumer_thread.start()

        producer_thread.join()
        consumer_thread.join()

        if interrupted_error_event.is_set():
            self._interrupt_post_processing()
            raise InterruptedError(
                "Conversation chain interrupted: consumer model interrupted"
            )

        print("\n\n --- Audio generation and playback completed ---")
        return full_response[0]

    def interrupt(self, heard_sentence: str = "") -> None:
        """Set the interrupt flag to stop the conversation chain.
        Preferably provide the sentences that were already shown or heard by the user before the interrupt so that the LLM can handle the memory properly.

        Parameters:
        - heard_sentence (str): The sentence that was already shown or heard by the user before the interrupt.
            (because apparently the user won't know the rest of the response.)
        """
        self._continue_exec_flag.clear()
        self.llm.handle_interrupt(heard_sentence)

    def _interrupt_post_processing(self) -> None:
        """Perform post-processing tasks (like resetting the continue flag to allow next conversation chain to start) after an interrupt."""
        #TODO # Stop any currently playing sound
        self._continue_exec_flag.set()  # Reset the interrupt flag

    def _check_interrupt(self):
        """Check if we are in an interrupt state and raise an exception if we are."""
        if not self._continue_exec_flag.is_set():
            raise InterruptedError("Conversation chain interrupted: checked")

    def is_complete_sentence(self, text: str):
        """
        Check if the text is a complete sentence.
        text: str
            the text to check
        """

        white_list = [
            "...",
            "Dr.",
            "Mr.",
            "Ms.",
            "Mrs.",
            "Jr.",
            "Sr.",
            "St.",
            "Ave.",
            "Rd.",
            "Blvd.",
            "Dept.",
            "Univ.",
            "Prof.",
            "Ph.D.",
            "M.D.",
            "U.S.",
            "U.K.",
            "U.N.",
            "E.U.",
            "U.S.A.",
            "U.K.",
            "U.S.S.R.",
            "U.A.E.",
            "NY."
        ]

        for item in white_list:
            if text.strip().lower().endswith(item.lower()):
                return False

        punctuation_blacklist = [
            ".",
            "?",
            "!",
            "。",
            "；",
            "？",
            "！",
            "…",
            "〰",
            "〜",
            "～",
            "！",
        ]
        return any(text.strip().endswith(punct) for punct in punctuation_blacklist)

    def clean_cache():
        cache_dir = "./cache"
        if os.path.exists(cache_dir):
            shutil.rmtree(cache_dir)
            os.makedirs(cache_dir)

if __name__ == "__main__": 
    with open("conf.yaml", "rb") as f:
        config = yaml.safe_load(f)

    vtuber_main = OpenLLMVTuberMain(config)
    
    atexit.register(vtuber_main.clean_cache)

    def _run_conversation_chain():
        try:
            vtuber_main.conversation_chain()
        except InterruptedError as e:
            print(f"😢Conversation was interrupted. {e}")

    while True:
        threading.Thread(target=_run_conversation_chain).start()

        if input(">>> say i and press enter to interrupt: ") == "i":
            print("\n\n!!!!!!!!!! interrupt !!!!!!!!!!!!...\n")
            vtuber_main.interrupt()
