module Lib
    ( someFunc
    , helperFunc
    , DemoData(..)
    , processData
    ) where

-- | A simple data type for testing
data DemoData = DemoData
    { dataField :: Int
    , dataName :: String
    } deriving (Show, Eq)

-- | Main exported function
someFunc :: IO ()
someFunc = do
    putStrLn "someFunc from Lib"
    helperFunc

-- | Helper function that is also exported
helperFunc :: IO ()
helperFunc = putStrLn "Helper function called"

-- | Process demo data
processData :: DemoData -> String
processData (DemoData field name) = 
    "Data: " ++ name ++ " with value " ++ show field

-- | Internal function not exported
internalFunc :: Int -> Int
internalFunc x = x * 2