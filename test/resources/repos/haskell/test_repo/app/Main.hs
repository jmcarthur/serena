module Main where

import Lib

main :: IO ()
main = do
    putStrLn "Hello from Haskell test repo!"
    someFunc
    let demo = DemoData 42 "Test"
    putStrLn $ processData demo
    useHelper

-- | Function that uses the helper
useHelper :: IO ()
useHelper = do
    putStrLn "About to call helper:"
    helperFunc